use std::{error::Error, fmt, sync::Arc};

use ga_archive::{Archive, ArchiveError, ArchiveKey, PutLimits};
use ga_domain::timestamp::StoredInstant;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, ReadinessError, check_readiness,
    garmin_owner::{OwnerVerificationError, verify_owner_in_transaction},
};

pub(crate) const GARMIN_LEGACY_SOURCE: &str = "garmin_connect";
const MUTATION_LOCK: i64 = 72104619;

pub struct LegacyCapture<'a> {
    pub authenticated_fingerprint: &'a str,
    pub endpoint: &'a str,
    pub source_key: &'a str,
    pub fetched_at: StoredInstant,
    pub body: Vec<u8>,
}

pub struct CapturedSource {
    pub id: Uuid,
    pub archive_key: ArchiveKey,
    pub newly_recorded: bool,
}

#[derive(Debug)]
pub enum CaptureError {
    UnknownEndpoint,
    CollectionDisabled,
    Busy,
    InputTooLarge,
    InvalidInput,
    Owner(OwnerVerificationError),
    Storage(ReadinessError),
    Archive(ArchiveError),
    ConflictingArchive,
    ArchiveTaskFailed,
    Database,
}

impl fmt::Display for CaptureError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::UnknownEndpoint => "Garmin JSON endpoint is unsupported",
            Self::CollectionDisabled => "Collection for this Garmin endpoint is disabled",
            Self::Busy => "Garmin source publication is busy",
            Self::InputTooLarge => "Garmin source payload exceeds its byte limit",
            Self::InvalidInput => "Garmin source payload is invalid",
            Self::Owner(_) => "Garmin owner could not be verified",
            Self::Storage(_) => "Garmin source storage is unavailable",
            Self::Archive(_) => "Garmin source could not be archived",
            Self::ConflictingArchive => "Recorded Garmin source archive does not match",
            Self::ArchiveTaskFailed => "Garmin source archive task ended unexpectedly",
            Self::Database => "Garmin source could not be recorded",
        })
    }
}
impl Error for CaptureError {}

impl From<sqlx::Error> for CaptureError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Store a complete legacy JSON response under its original Python content identity.
    /// This path leaves the row pending and does not publish observations or freshness.
    pub async fn capture_legacy_json(
        &self,
        request: LegacyCapture<'_>,
        archive: Arc<Archive>,
        limits: PutLimits,
    ) -> Result<CapturedSource, CaptureError> {
        if request.body.len() as u64 > limits.max_bytes {
            return Err(CaptureError::InputTooLarge);
        }
        let body = String::from_utf8(request.body).map_err(|_| CaptureError::InvalidInput)?;
        let txn = self
            .begin_legacy_source(
                request.authenticated_fingerprint,
                request.endpoint,
                request.source_key,
            )
            .await?;

        // Keep the authorization transaction alive until blocking filesystem work stops,
        // even if the async caller is cancelled while the archive is being written.
        let (mut txn, archive_key, body) = tokio::task::spawn_blocking(move || {
            let key = archive
                .put_json(body.as_bytes(), limits)
                .map_err(CaptureError::Archive)?;
            Ok::<_, CaptureError>((txn, key, body))
        })
        .await
        .map_err(|_| CaptureError::ArchiveTaskFailed)??;
        let capture = insert_source(
            &mut txn,
            request.endpoint,
            request.source_key,
            request.fetched_at,
            archive_key,
            &body,
        )
        .await?;
        txn.commit().await?;
        Ok(capture)
    }
    pub(crate) async fn begin_legacy_source(
        &self,
        fingerprint: &str,
        endpoint: &str,
        source_key: &str,
    ) -> Result<Transaction<'static, Postgres>, CaptureError> {
        let packs = required_packs(endpoint).ok_or(CaptureError::UnknownEndpoint)?;
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(CaptureError::Storage)?;
        authorize_legacy_mutation(&mut txn, fingerprint, packs).await?;
        let scope = format!("{GARMIN_LEGACY_SOURCE}:{}:{}", endpoint, source_key);
        lock_legacy_scope(&mut txn, &scope).await?;
        Ok(txn)
    }
}

pub(crate) async fn authorize_legacy_mutation(
    txn: &mut Transaction<'_, Postgres>,
    fingerprint: &str,
    packs: &[&str],
) -> Result<(), CaptureError> {
    let owner = verify_owner_in_transaction(txn, fingerprint)
        .await
        .map_err(CaptureError::Owner)?;
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(MUTATION_LOCK)
        .fetch_one(&mut **txn)
        .await?;
    if !locked {
        return Err(CaptureError::Busy);
    }
    let allowed: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM module_configs WHERE owner_id = $1)
             OR NOT EXISTS (
                SELECT 1 FROM unnest($2::text[]) AS required(pack_key)
                WHERE NOT EXISTS (SELECT 1 FROM module_configs AS config
                   WHERE config.owner_id = $1 AND config.pack_key = required.pack_key
                     AND config.collection_enabled)
             )",
    )
    .bind(owner.owner_id())
    .bind(packs)
    .fetch_one(&mut **txn)
    .await?;
    if !allowed {
        return Err(CaptureError::CollectionDisabled);
    }
    Ok(())
}

pub(crate) async fn lock_legacy_scope(
    txn: &mut Transaction<'_, Postgres>,
    scope: &str,
) -> Result<(), CaptureError> {
    let locked: bool =
        sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(scope)
            .fetch_one(&mut **txn)
            .await?;
    if !locked {
        return Err(CaptureError::Busy);
    }

    Ok(())
}

async fn insert_source(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &str,
    source_key: &str,
    fetched_at: StoredInstant,
    archive_key: ArchiveKey,
    body: &str,
) -> Result<CapturedSource, CaptureError> {
    let digest = archive_key.digest_hex();
    let key = archive_key.to_string();
    let inserted_id = sqlx::query_scalar(
        "INSERT INTO source_payloads (id, source, endpoint, source_key, payload_hash, payload,
                 archive_key, fetched_at, status, parser_version)
         VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 'pending', 0)
         ON CONFLICT (source, endpoint, source_key, payload_hash) DO NOTHING RETURNING id",
    )
    .bind(Uuid::new_v4())
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(endpoint)
    .bind(source_key)
    .bind(&digest)
    .bind(body)
    .bind(&key)
    .bind(OffsetDateTime::from(fetched_at))
    .fetch_optional(&mut **txn)
    .await?;
    if let Some(id) = inserted_id {
        return Ok(CapturedSource {
            id,
            archive_key,
            newly_recorded: true,
        });
    }
    let (id, recorded_key) = sqlx::query_as::<_, (Uuid, String)>(
        "SELECT id, archive_key FROM source_payloads WHERE source = $1 AND endpoint = $2
             AND source_key = $3 AND payload_hash = $4",
    )
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(endpoint)
    .bind(source_key)
    .bind(digest)
    .fetch_one(&mut **txn)
    .await?;
    if recorded_key != key {
        return Err(CaptureError::ConflictingArchive);
    }
    Ok(CapturedSource {
        id,
        archive_key,
        newly_recorded: false,
    })
}

fn required_packs(endpoint: &str) -> Option<&'static [&'static str]> {
    match endpoint {
        "daily" => Some(&["training", "wellbeing"]),
        "heart_rate"
        | "hrv"
        | "stress"
        | "body_battery"
        | "body_battery_events"
        | "respiration"
        | "spo2"
        | "body_composition"
        | "resting_hr" => Some(&["wellbeing"]),
        "sleep" => Some(&["sleep"]),
        "hydration" => Some(&["general_diary"]),
        "all_day_events" => Some(&["general_diary", "wellbeing", "sleep", "training"]),
        "steps"
        | "readiness"
        | "training_status"
        | "max_metrics"
        | "endurance"
        | "hill"
        | "intensity"
        | "devices"
        | "activities"
        | "activity"
        | "activity_details"
        | "activity_splits"
        | "activity_typed_splits"
        | "activity_zones"
        | "activity_weather" => Some(&["training"]),
        _ => None,
    }
}
