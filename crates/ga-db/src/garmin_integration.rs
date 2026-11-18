mod reauthentication;

pub use reauthentication::{GarminReauthError, GarminReauthStatus, GarminReauthTarget};

use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{Executor, Postgres, Transaction};

use crate::{
    Database, ReadinessError, check_readiness,
    garmin_owner::{
        OwnerVerificationError, stored_garmin_fingerprint, verify_owner_in_transaction,
    },
    jobs::STORAGE_QUEUE_LOCK,
    scenario_packs::source_instance_selected,
    share_policy::OWNER_ONBOARDING_LOCK,
};

const GARMIN_STATE_KEY: &str = "integration:garmin";
const MINIMUM_RATE_LIMIT: Duration = Duration::from_secs(900);
const MAXIMUM_PROVIDER_DELAY: Duration = Duration::from_secs(7 * 86_400);

#[derive(Clone, Copy, Debug)]
pub enum GarminTransportOutcome {
    Reachable,
    AuthorizedLogin,
    AuthenticationRequired,
    AccountMismatch,
    RateLimited { retry_after: Option<Duration> },
    ConnectionFailure { retry_after: Option<Duration> },
    CircuitOpen,
}

#[derive(Clone, Copy, Debug)]
pub enum GarminRequestPause {
    Reauthentication,
    Cooldown(StoredInstant),
}

pub enum GarminSourceAdmission {
    Unselected,
    Unbound,
    Paused(GarminRequestPause),
    Ready { fingerprint: String },
}

pub struct RecordedGarminTransport {
    pub status: String,
    pub blocked_until: Option<StoredInstant>,
    pub failure_count: u32,
}

#[derive(Debug)]
pub enum GarminIntegrationError {
    Storage(ReadinessError),
    Owner(OwnerVerificationError),
    Busy,
    InvalidClock,
    InvalidState,
    Database,
}

impl fmt::Display for GarminIntegrationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Garmin integration storage is unavailable",
            Self::Owner(_) => "Garmin integration owner could not be verified",
            Self::Busy => "Garmin integration state is busy",
            Self::InvalidClock => "Garmin integration clock cannot be represented",
            Self::InvalidState => "Saved Garmin connection state is invalid",
            Self::Database => "Garmin integration state could not be stored",
        })
    }
}

impl Error for GarminIntegrationError {}

impl From<sqlx::Error> for GarminIntegrationError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Read the currently selected source, owner binding and pause state before
    /// the worker opens a local Garmin session or schedules any provider work.
    pub async fn garmin_source_admission(
        &self,
        instance: &str,
        now: StoredInstant,
    ) -> Result<GarminSourceAdmission, GarminIntegrationError> {
        if instance.is_empty() || instance.len() > 200 {
            return Err(GarminIntegrationError::InvalidState);
        }
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(GarminIntegrationError::Storage)?;
            let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock_shared($1)")
                .bind(OWNER_ONBOARDING_LOCK)
                .fetch_one(&mut *txn)
                .await?;
            if !locked {
                return Err(GarminIntegrationError::Busy);
            }
            if !source_instance_selected(&mut txn, instance).await? {
                return Ok(GarminSourceAdmission::Unselected);
            }
            let Some(fingerprint) = stored_garmin_fingerprint(&mut txn)
                .await
                .map_err(GarminIntegrationError::Owner)?
            else {
                return Ok(GarminSourceAdmission::Unbound);
            };
            if let Some(pause) = garmin_pause(&mut *txn, now).await? {
                return Ok(GarminSourceAdmission::Paused(pause));
            }
            txn.commit().await?;
            Ok(GarminSourceAdmission::Ready { fingerprint })
        })
        .await
        .map_err(|_| GarminIntegrationError::Database)?
    }

    /// Check a durable pause again before starting an already leased request.
    pub async fn garmin_request_pause(
        &self,
        at: StoredInstant,
    ) -> Result<Option<GarminRequestPause>, GarminIntegrationError> {
        tokio::time::timeout(Duration::from_millis(750), garmin_pause(&self.control, at))
            .await
            .map_err(|_| GarminIntegrationError::Database)?
    }

    /// Persist the outcome of a single authorized Garmin transport operation.
    pub async fn record_garmin_transport(
        &self,
        expected_fingerprint: &str,
        at: StoredInstant,
        outcome: GarminTransportOutcome,
    ) -> Result<RecordedGarminTransport, GarminIntegrationError> {
        let mut txn = self.control.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(GarminIntegrationError::Storage)?;
        verify_owner_in_transaction(&mut txn, expected_fingerprint)
            .await
            .map_err(GarminIntegrationError::Owner)?;
        let admitted: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(STORAGE_QUEUE_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !admitted {
            return Err(GarminIntegrationError::Busy);
        }
        let state = record_transport_in(&mut txn, at, outcome).await?;
        txn.commit().await?;
        Ok(state)
    }
}

async fn record_transport_in(
    txn: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
    outcome: GarminTransportOutcome,
) -> Result<RecordedGarminTransport, GarminIntegrationError> {
    let previous = recorded_state(txn).await?;
    let old_count = previous
        .as_ref()
        .map_or(0, |(state, _)| state.failure_count);
    if let Some((state, previous_at)) = previous
        && (previous_at.is_some_and(|previous_at| previous_at > at)
            || (state.status == "reauth_required"
                && !matches!(
                    outcome,
                    GarminTransportOutcome::AuthorizedLogin
                        | GarminTransportOutcome::AuthenticationRequired
                        | GarminTransportOutcome::AccountMismatch
                )))
    {
        return Ok(state);
    }
    let (status, reason, retry_after, minimum) = match outcome {
        GarminTransportOutcome::Reachable | GarminTransportOutcome::AuthorizedLogin => {
            ("active", None, None, None)
        }
        GarminTransportOutcome::AuthenticationRequired => (
            "reauth_required",
            Some("AuthenticationRequired"),
            None,
            None,
        ),
        GarminTransportOutcome::AccountMismatch => {
            ("reauth_required", Some("AccountMismatch"), None, None)
        }
        GarminTransportOutcome::RateLimited { retry_after } => (
            "rate_limited",
            Some("GarminConnectTooManyRequestsError"),
            retry_after,
            Some(MINIMUM_RATE_LIMIT),
        ),
        GarminTransportOutcome::ConnectionFailure { retry_after } => (
            "degraded",
            Some("GarminConnectConnectionError"),
            retry_after,
            Some(Duration::ZERO),
        ),
        GarminTransportOutcome::CircuitOpen => (
            "degraded",
            Some("CircuitOpen"),
            None,
            Some(MINIMUM_RATE_LIMIT),
        ),
    };
    let blocked_until = match (minimum, retry_after) {
        (None, _) => None,
        (Some(minimum), Some(delay)) => {
            let delay = delay
                .clamp(Duration::from_secs(1), MAXIMUM_PROVIDER_DELAY)
                .max(minimum);
            Some(deadline(at, delay)?)
        }
        (Some(minimum), None) => {
            let jitter: f64 = sqlx::query_scalar("SELECT random() * 5.0")
                .fetch_one(&mut **txn)
                .await?;
            let seconds = f64::from((60_u32 << old_count.min(6)).min(3_600)) + jitter;
            Some(deadline(at, Duration::from_secs_f64(seconds).max(minimum))?)
        }
    };
    let failure_count = if reason.is_some() {
        old_count.saturating_add(1)
    } else {
        0
    };
    sqlx::query(
        "INSERT INTO app_state(key, value) VALUES ($1,$2::jsonb)
             ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
    )
    .bind(GARMIN_STATE_KEY)
    .bind(
        json!({
            "status": status,
            "blocked_until": blocked_until.map(|at| at.to_string()),
            "last_attempt": at.to_string(),
            "reason_class": reason,
            "failure_count": failure_count,
        })
        .to_string(),
    )
    .execute(&mut **txn)
    .await?;
    Ok(RecordedGarminTransport {
        status: status.to_owned(),
        blocked_until,
        failure_count,
    })
}

pub(crate) async fn garmin_pause<'e>(
    executor: impl Executor<'e, Database = Postgres>,
    at: StoredInstant,
) -> Result<Option<GarminRequestPause>, GarminIntegrationError> {
    let state: Option<(Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT value->>'status', NULLIF(value->>'blocked_until','')
         FROM app_state WHERE key=$1",
    )
    .bind(GARMIN_STATE_KEY)
    .fetch_optional(executor)
    .await?;
    let Some((status, deadline)) = state else {
        return Ok(None);
    };
    if status.as_deref() == Some("reauth_required") {
        return Ok(Some(GarminRequestPause::Reauthentication));
    }
    let deadline = deadline
        .map(|value| value.parse::<StoredInstant>())
        .transpose()
        .map_err(|_| GarminIntegrationError::InvalidState)?;
    Ok(deadline
        .filter(|deadline| *deadline > at)
        .map(GarminRequestPause::Cooldown))
}

async fn recorded_state(
    txn: &mut sqlx::Transaction<'_, sqlx::Postgres>,
) -> Result<Option<(RecordedGarminTransport, Option<StoredInstant>)>, GarminIntegrationError> {
    type State = (Option<String>, Option<String>, Option<String>, i64);
    let state = sqlx::query_as::<_, State>(
        "SELECT value->>'status', nullif(value->>'blocked_until',''), nullif(value->>'last_attempt',''),
             coalesce((value->>'failure_count')::bigint, 0)
         FROM app_state WHERE key = $1 FOR UPDATE",
    )
    .bind(GARMIN_STATE_KEY)
    .fetch_optional(&mut **txn)
    .await?;
    let parse = |at: Option<String>| {
        at.map(|at| {
            at.parse::<StoredInstant>()
                .map_err(|_| GarminIntegrationError::InvalidState)
        })
        .transpose()
    };
    state
        .map(|(status, blocked, last, count)| {
            Ok((
                RecordedGarminTransport {
                    status: status.unwrap_or_default(),
                    blocked_until: parse(blocked)?,
                    failure_count: u32::try_from(count)
                        .map_err(|_| GarminIntegrationError::InvalidState)?,
                },
                parse(last)?,
            ))
        })
        .transpose()
}

fn deadline(at: StoredInstant, delay: Duration) -> Result<StoredInstant, GarminIntegrationError> {
    let micros =
        i64::try_from(delay.as_micros()).map_err(|_| GarminIntegrationError::InvalidClock)?;
    at.unix_microseconds()
        .checked_add(micros)
        .and_then(|deadline| StoredInstant::from_unix_microseconds(deadline).ok())
        .ok_or(GarminIntegrationError::InvalidClock)
}
