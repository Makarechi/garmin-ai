use ga_domain::{
    activity_scan::{
        ACTIVITY_PAGE_SIZE, ActivityScanAdvance, ActivityScanPage, advance_activity_scan,
    },
    event_input::{known_timezone, source_calendar_date},
    garmin_summary::{activity_identity, gmt_instant},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime, format_description::well_known::Iso8601};
use uuid::Uuid;

use super::{ActivityScanError, MUTATION_LOCK, collection_enabled, enqueue_page};
use crate::{
    Database,
    activity_documents::{
        ActivitySummaryKind, ApplyActivitySummary, DocumentError, DocumentStatus, put_state,
    },
    check_readiness,
    garmin_freshness::record_freshness,
    garmin_owner::verify_owner_in_transaction,
    jobs::{FailureReason, GarminJobKind, GarminJobLease, JobOutcome},
    raw_capture::{GARMIN_LEGACY_SOURCE, lock_legacy_scope},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityPagePreflight {
    Fetch { offset: u32 },
    Superseded,
    CollectionDisabled,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityPageResult {
    Published(DocumentStatus),
    Retry(DocumentStatus),
    Superseded,
    CollectionDisabled,
}

pub struct PublishActivityPage<'a> {
    pub authenticated_fingerprint: &'a str,
    pub lease: &'a GarminJobLease,
    pub source_id: Uuid,
    pub requested_at: StoredInstant,
    pub timezone: &'a str,
}

struct Page {
    payload: Value,
    offset: u32,
    since: Date,
    backfill: bool,
    track: Option<TrackedPage>,
}

struct TrackedPage {
    key: String,
    generation: Uuid,
    round: u8,
    anchor: Vec<String>,
}

impl Page {
    fn from_lease(lease: &GarminJobLease, account: &str) -> Result<Self, ActivityScanError> {
        let payload: Value = serde_json::from_str(lease.payload_json())
            .map_err(|_| ActivityScanError::InvalidState)?;
        if lease.kind() != GarminJobKind::Activities
            || payload
                .get("account")
                .is_some_and(|value| value.as_str() != Some(account))
        {
            return Err(ActivityScanError::InvalidState);
        }
        let offset = payload["offset"]
            .as_u64()
            .and_then(|offset| u32::try_from(offset).ok())
            .ok_or(ActivityScanError::InvalidState)?;
        let since = payload["since"]
            .as_str()
            .and_then(|date| Date::parse(date, &Iso8601::DATE).ok())
            .ok_or(ActivityScanError::InvalidState)?;
        let backfill = payload["backfill"].as_bool().unwrap_or(false);
        let lane = if backfill { "history" } else { "recent" };
        let track = match payload.get("scan_key") {
            None | Some(Value::Null) => None,
            Some(Value::String(key))
                if *key == format!("activity_scan:{account}:{lane}")
                    && payload["account"].as_str() == Some(account) =>
            {
                let generation = payload["generation"]
                    .as_str()
                    .and_then(|id| Uuid::parse_str(id).ok())
                    .ok_or(ActivityScanError::InvalidState)?;
                let round = payload["round"]
                    .as_u64()
                    .and_then(|value| u8::try_from(value).ok())
                    .ok_or(ActivityScanError::InvalidState)?;
                let anchor = payload["anchor"]
                    .as_array()
                    .filter(|items| items.len() <= ACTIVITY_PAGE_SIZE)
                    .and_then(|items| {
                        items
                            .iter()
                            .map(|item| item.as_str().map(str::to_owned))
                            .collect::<Option<Vec<_>>>()
                    })
                    .ok_or(ActivityScanError::InvalidState)?;
                Some(TrackedPage {
                    key: key.clone(),
                    generation,
                    round,
                    anchor,
                })
            }
            _ => return Err(ActivityScanError::InvalidState),
        };
        Ok(Self {
            payload,
            offset,
            since,
            backfill,
            track,
        })
    }

    fn source_key(&self) -> String {
        format!("page:{}", self.offset)
    }

    async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        lease: &GarminJobLease,
    ) -> Result<Option<Value>, ActivityScanError> {
        let Some(track) = &self.track else {
            return Ok(Some(Value::Null));
        };
        let state: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1 FOR UPDATE")
                .bind(&track.key)
                .fetch_optional(&mut **txn)
                .await?;
        let Some(state) = state else {
            return Ok(None);
        };
        let state: Value =
            serde_json::from_str(&state).map_err(|_| ActivityScanError::InvalidState)?;
        let same = state["status"] == "running"
            && state["job_id"] == lease.id().to_string()
            && [
                "account",
                "generation",
                "round",
                "offset",
                "anchor",
                "since",
                "backfill",
            ]
            .iter()
            .all(|field| state[*field] == self.payload[*field]);
        Ok(same.then_some(state))
    }
}

impl Database {
    /// Verify this lease and its saved cursor before requesting a page from Garmin.
    pub async fn preflight_activity_page(
        &self,
        authenticated_fingerprint: &str,
        lease: &GarminJobLease,
        requested_at: StoredInstant,
    ) -> Result<ActivityPagePreflight, ActivityScanError> {
        let page = Page::from_lease(lease, authenticated_fingerprint)?;
        let (mut txn, enabled) = self
            .begin_activity_page(authenticated_fingerprint, lease)
            .await?;
        let outcome = match page.current(&mut txn, lease).await? {
            None => ActivityPagePreflight::Superseded,
            Some(state) if !enabled => {
                disable(&mut txn, &page, state, requested_at).await?;
                ActivityPagePreflight::CollectionDisabled
            }
            Some(_) => ActivityPagePreflight::Fetch {
                offset: page.offset,
            },
        };
        if !matches!(outcome, ActivityPagePreflight::Fetch { .. }) {
            lease
                .finish_in(&mut txn, JobOutcome::Completed)
                .await
                .map_err(ActivityScanError::Job)?;
        }
        txn.commit().await?;
        Ok(outcome)
    }

    /// Commit an archived Garmin list, activity jobs, scan cursor and lease result together.
    pub async fn publish_activity_page(
        &self,
        request: PublishActivityPage<'_>,
    ) -> Result<ActivityPageResult, ActivityScanError> {
        if !known_timezone(request.timezone) {
            return Err(ActivityScanError::InvalidHorizon);
        }
        let page = Page::from_lease(request.lease, request.authenticated_fingerprint)?;
        let (mut txn, enabled) = self
            .begin_activity_page(request.authenticated_fingerprint, request.lease)
            .await?;
        let result = match page.current(&mut txn, request.lease).await? {
            None => ActivityPageResult::Superseded,
            Some(state) if !enabled => {
                disable(&mut txn, &page, state, request.requested_at).await?;
                ActivityPageResult::CollectionDisabled
            }
            Some(state) => publish_page(&mut txn, &page, state, &request).await?,
        };
        let outcome = if matches!(result, ActivityPageResult::Retry(_)) {
            JobOutcome::Failed {
                reason: FailureReason::InvalidGarminWork,
                retry_at: None,
            }
        } else {
            JobOutcome::Completed
        };
        request
            .lease
            .finish_in(&mut txn, outcome)
            .await
            .map_err(ActivityScanError::Job)?;
        txn.commit().await?;
        Ok(result)
    }

    /// Finish a failed read without advancing the cursor or clearing earlier successes.
    pub async fn finish_activity_page_fetch_error(
        &self,
        authenticated_fingerprint: &str,
        lease: &GarminJobLease,
        requested_at: StoredInstant,
        reason: FailureReason,
        retry_at: Option<StoredInstant>,
    ) -> Result<(), ActivityScanError> {
        let page = Page::from_lease(lease, authenticated_fingerprint)?;
        let (mut txn, enabled) = self
            .begin_activity_page(authenticated_fingerprint, lease)
            .await?;
        let outcome = match page.current(&mut txn, lease).await? {
            None => JobOutcome::Completed,
            Some(state) if !enabled => {
                disable(&mut txn, &page, state, requested_at).await?;
                JobOutcome::Completed
            }
            Some(_) => {
                record_freshness(
                    &mut txn,
                    "activities",
                    &page.source_key(),
                    "fetch_error",
                    requested_at,
                    None,
                )
                .await?;
                JobOutcome::Failed { reason, retry_at }
            }
        };
        lease
            .finish_in(&mut txn, outcome)
            .await
            .map_err(ActivityScanError::Job)?;
        txn.commit().await?;
        Ok(())
    }

    async fn begin_activity_page(
        &self,
        account: &str,
        lease: &GarminJobLease,
    ) -> Result<(Transaction<'static, Postgres>, bool), ActivityScanError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(ActivityScanError::Storage)?;
        let owner = verify_owner_in_transaction(&mut txn, account)
            .await
            .map_err(ActivityScanError::Owner)?;
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(MUTATION_LOCK)
            .fetch_one(&mut *txn)
            .await?;
        if !locked {
            return Err(ActivityScanError::Busy);
        }
        lease
            .check_live(&mut txn)
            .await
            .map_err(ActivityScanError::Job)?;
        let allowed = collection_enabled(&mut txn, owner.owner_id()).await?;
        Ok((txn, allowed))
    }
}

async fn disable(
    txn: &mut Transaction<'_, Postgres>,
    page: &Page,
    mut state: Value,
    now: StoredInstant,
) -> Result<(), ActivityScanError> {
    if let Some(track) = &page.track {
        state["status"] = json!("disabled");
        state["next_scan_at"] = Value::Null;
        state["disabled_at"] = json!(now.to_string());
        put_state(txn, &track.key, &state).await?;
    }
    Ok(())
}

async fn publish_page(
    txn: &mut Transaction<'_, Postgres>,
    page: &Page,
    state: Value,
    request: &PublishActivityPage<'_>,
) -> Result<ActivityPageResult, ActivityScanError> {
    let source_key = page.source_key();
    lock_legacy_scope(
        txn,
        &format!("{GARMIN_LEGACY_SOURCE}:activities:{source_key}"),
    )
    .await
    .map_err(|error| ActivityScanError::Document(DocumentError::Access(error)))?;
    let body: String = sqlx::query_scalar(
        "SELECT payload::text FROM source_payloads
         WHERE id=$1 AND source=$2 AND endpoint='activities' AND source_key=$3 AND payload IS NOT NULL FOR UPDATE",
    )
    .bind(request.source_id)
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(&source_key)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(ActivityScanError::Document(DocumentError::SourceMissing))?;
    let values: Value = serde_json::from_str(&body).map_err(|_| ActivityScanError::InvalidState)?;
    let status = Database::apply_activity_in(
        txn,
        ApplyActivitySummary {
            account_fingerprint: request.authenticated_fingerprint,
            source_key: &source_key,
            summary: ActivitySummaryKind::Page,
            source_id: request.source_id,
            requested_at: request.requested_at,
            timezone: request.timezone,
        }
        .into(),
    )
    .await
    .map_err(ActivityScanError::Document)?;
    let status = if values.is_array() {
        status
    } else {
        DocumentStatus::Error
    };
    record_freshness(
        txn,
        "activities",
        &source_key,
        status.label(),
        request.requested_at,
        Some(request.source_id),
    )
    .await?;
    if status == DocumentStatus::Error || (status == DocumentStatus::Stale && page.track.is_some())
    {
        return Ok(ActivityPageResult::Retry(status));
    }
    if status != DocumentStatus::Stale {
        let values = values.as_array().ok_or(ActivityScanError::InvalidState)?;
        advance(txn, page, state, values, request).await?;
    }
    Ok(ActivityPageResult::Published(status))
}

async fn advance(
    txn: &mut Transaction<'_, Postgres>,
    page: &Page,
    mut state: Value,
    values: &[Value],
    request: &PublishActivityPage<'_>,
) -> Result<(), ActivityScanError> {
    let mut identities = Vec::new();
    for value in values {
        if observed_on(value, request.timezone)? >= page.since {
            identities.push(activity_identity(value).map_err(|_| ActivityScanError::InvalidState)?);
        }
    }
    sqlx::query(include_str!("queue_activity_followups.sql"))
        .bind(&identities)
        .bind(request.authenticated_fingerprint)
        .bind(page.backfill)
        .bind(OffsetDateTime::from(request.requested_at))
        .execute(&mut **txn)
        .await?;
    let Some(track) = &page.track else {
        if values.len() == ACTIVITY_PAGE_SIZE
            && observed_on(&values[ACTIVITY_PAGE_SIZE - 1], request.timezone)? >= page.since
        {
            let mut following = page.payload.clone();
            let offset = page
                .offset
                .checked_add(ACTIVITY_PAGE_SIZE as u32)
                .ok_or(ActivityScanError::InvalidState)?;
            following["offset"] = json!(offset);
            let date = OffsetDateTime::from(request.requested_at).date();
            enqueue_page(
                txn,
                &following,
                &format!("activity-page:{date}:{offset}"),
                request.requested_at,
            )
            .await?;
        }
        return Ok(());
    };
    let cursor = ActivityScanPage {
        offset: page.offset,
        round: track.round,
        anchor: &track.anchor,
        since: page.since,
        backfill: page.backfill,
    };
    let pages = state["pages"]
        .as_u64()
        .and_then(|pages| pages.checked_add(1))
        .ok_or(ActivityScanError::InvalidState)?;
    state["pages"] = json!(pages);
    state["updated_at"] = json!(request.requested_at.to_string());
    match advance_activity_scan(cursor, values, request.timezone, request.requested_at)
        .map_err(|_| ActivityScanError::InvalidState)?
    {
        ActivityScanAdvance::Following {
            offset,
            round,
            anchor,
        } => {
            let mut following = page.payload.clone();
            following["offset"] = json!(offset);
            following["round"] = json!(round);
            following["anchor"] = json!(anchor);
            let dedup = format!(
                "activity-page:{}:{}:{round}:{offset}",
                request.authenticated_fingerprint, track.generation
            );
            let id = enqueue_page(txn, &following, &dedup, request.requested_at).await?;
            for field in ["offset", "round", "anchor"] {
                state[field] = following[field].take();
            }
            state["job_id"] = json!(id.to_string());
        }
        ActivityScanAdvance::Complete { next_scan_at } => {
            state["status"] = json!("complete");
            state["completed_at"] = json!(request.requested_at.to_string());
            state["next_scan_at"] = json!(next_scan_at.to_string());
        }
        ActivityScanAdvance::Unstable { next_scan_at } => {
            state["status"] = json!("unstable_inventory");
            state["next_scan_at"] = json!(next_scan_at.to_string());
        }
    }
    put_state(txn, &track.key, &state).await?;
    Ok(())
}

fn observed_on(value: &Value, timezone: &str) -> Result<Date, ActivityScanError> {
    gmt_instant(&value["startTimeGMT"])
        .and_then(|instant| source_calendar_date(instant, timezone))
        .ok_or(ActivityScanError::InvalidState)
}
