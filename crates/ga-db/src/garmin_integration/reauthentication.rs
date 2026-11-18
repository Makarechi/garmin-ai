use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    GARMIN_STATE_KEY, GarminIntegrationError, GarminTransportOutcome, record_transport_in,
};
use crate::{
    Database,
    channel_ingress::telegram_poll::{TelegramPollError, selection},
    check_readiness,
    database_export::TOKEN_PUBLICATION_LOCK,
    garmin_owner::{
        OwnerVerificationError, stored_garmin_fingerprint, verify_owner_in_transaction,
    },
    jobs::STORAGE_QUEUE_LOCK,
    scenario_packs::source_instance_selected,
    telegram_binding::{TelegramBindingError, confirmed_pairing},
};

#[derive(Clone, Copy)]
pub struct GarminReauthTarget<'a> {
    pub telegram_instance: &'a str,
    pub telegram_owner: i64,
    pub garmin_instance: &'a str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GarminReauthStatus {
    Active,
    Required,
    Unavailable,
}

#[derive(Debug)]
pub enum GarminReauthError {
    Integration(GarminIntegrationError),
    Channel(TelegramPollError),
    Pairing(TelegramBindingError),
    Unavailable,
    Publication,
}

impl fmt::Display for GarminReauthError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Integration(error) => error.fmt(f),
            Self::Channel(error) => error.fmt(f),
            Self::Pairing(error) => error.fmt(f),
            Self::Unavailable => f.write_str("Garmin web reauthentication is unavailable"),
            Self::Publication => f.write_str("Garmin login could not be saved"),
        }
    }
}
impl Error for GarminReauthError {}
impl From<GarminIntegrationError> for GarminReauthError {
    fn from(error: GarminIntegrationError) -> Self {
        Self::Integration(error)
    }
}
impl From<sqlx::Error> for GarminReauthError {
    fn from(error: sqlx::Error) -> Self {
        Self::Integration(error.into())
    }
}

impl Database {
    /// Verify current ownership, source selection and the recorded login requirement.
    pub async fn garmin_web_reauth_status(
        &self,
        target: GarminReauthTarget<'_>,
    ) -> Result<GarminReauthStatus, GarminReauthError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            let (_, status) = current_reauth_status(&mut txn, target).await?;
            txn.commit().await?;
            Ok(status)
        })
        .await
        .map_err(|_| GarminIntegrationError::Database)?
    }

    /// Publish tokens for the verified account while owner changes and backup are fenced.
    /// The recorded state changes only after the local writer confirms durable publication.
    pub async fn commit_garmin_web_reauth(
        &self,
        target: GarminReauthTarget<'_>,
        fingerprint: &str,
        publish: impl FnOnce() -> bool,
    ) -> Result<(), GarminReauthError> {
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.control.begin().await?;
            for lock in [TOKEN_PUBLICATION_LOCK, STORAGE_QUEUE_LOCK] {
                let held: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                    .bind(lock)
                    .fetch_one(&mut *txn)
                    .await?;
                if !held {
                    return Err(GarminIntegrationError::Busy.into());
                }
            }
            let (telegram_owner, status) = current_reauth_status(&mut txn, target).await?;
            if status != GarminReauthStatus::Required {
                return Err(GarminReauthError::Unavailable);
            }
            let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *txn)
                .await?;
            let at =
                StoredInstant::try_from(at).map_err(|_| GarminIntegrationError::InvalidClock)?;
            match verify_owner_in_transaction(&mut txn, fingerprint).await {
                Ok(garmin_owner) if telegram_owner == garmin_owner.owner_id() => {}
                Ok(_) | Err(OwnerVerificationError::Mismatch) => {
                    record_transport_in(&mut txn, at, GarminTransportOutcome::AccountMismatch)
                        .await?;
                    txn.commit().await?;
                    return Err(
                        GarminIntegrationError::Owner(OwnerVerificationError::Mismatch).into(),
                    );
                }
                Err(error) => return Err(GarminIntegrationError::Owner(error).into()),
            }
            let state =
                record_transport_in(&mut txn, at, GarminTransportOutcome::AuthorizedLogin).await?;
            if state.status != "active" {
                return Err(GarminReauthError::Unavailable);
            }
            if !publish() {
                return Err(GarminReauthError::Publication);
            }
            txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| GarminIntegrationError::Database)?
    }
}

async fn current_reauth_status(
    txn: &mut Transaction<'_, Postgres>,
    target: GarminReauthTarget<'_>,
) -> Result<(Uuid, GarminReauthStatus), GarminReauthError> {
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut **txn)
        .await?;
    check_readiness(txn)
        .await
        .map_err(GarminIntegrationError::Storage)?;
    if !selection(txn, target.telegram_instance)
        .await
        .map_err(GarminReauthError::Channel)?
        || !source_instance_selected(txn, target.garmin_instance).await?
    {
        return Err(GarminReauthError::Unavailable);
    }
    let owner = confirmed_pairing(txn, target.telegram_instance, target.telegram_owner)
        .await
        .map_err(GarminReauthError::Pairing)?;
    if stored_garmin_fingerprint(txn)
        .await
        .map_err(GarminIntegrationError::Owner)?
        .is_none()
    {
        return Err(
            GarminIntegrationError::Owner(OwnerVerificationError::EnrollmentRequired).into(),
        );
    }
    let state: Option<(Option<String>, String)> = sqlx::query_as(
        "SELECT value->>'status',coalesce(value->>'reason_class','')
         FROM app_state WHERE key=$1",
    )
    .bind(GARMIN_STATE_KEY)
    .fetch_optional(&mut **txn)
    .await?;
    let status = match state {
        Some((Some(status), _)) if status == "active" => GarminReauthStatus::Active,
        Some((Some(status), reason))
            if status == "reauth_required" && !reason.starts_with("Account") =>
        {
            GarminReauthStatus::Required
        }
        _ => GarminReauthStatus::Unavailable,
    };
    Ok((owner, status))
}
