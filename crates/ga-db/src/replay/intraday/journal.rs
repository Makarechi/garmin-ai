use std::{
    io::{BufReader, Read},
    sync::Arc,
};

use ga_archive::{Archive, ArchiveKey};
use ga_domain::{
    event_input::known_timezone, garmin_health::HealthReplacement, timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::super::ReplayInputError;
use crate::raw_capture::GARMIN_LEGACY_SOURCE;

const APPLICATION_LIMIT: usize = 1_000;

pub(super) enum Application {
    UnknownOrder,
    Verified {
        raw_id: Uuid,
        at: StoredInstant,
        timezone: String,
        replacement: Option<HealthReplacement>,
    },
}

pub(super) async fn load(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &str,
    key: &str,
) -> Result<Option<Vec<Application>>, ReplayInputError> {
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(format!(
                "ingest-history:{GARMIN_LEGACY_SOURCE}:{endpoint}:{key}"
            ))
            .fetch_optional(&mut **txn)
            .await?;
    let Some(stored) = stored else {
        return Ok(None);
    };
    let stored: Value =
        serde_json::from_str(&stored).map_err(|_| ReplayInputError::HistoricalContract)?;
    let entries = stored["applications"]
        .as_array()
        .filter(|entries| entries.len() <= APPLICATION_LIMIT)
        .ok_or(ReplayInputError::HistoricalContract)?;
    entries
        .iter()
        .map(|entry| parse(entry, endpoint))
        .collect::<Result<Vec<_>, _>>()
        .map(Some)
}

fn parse(entry: &Value, endpoint: &str) -> Result<Application, ReplayInputError> {
    if entry["legacy_order_unknown"].as_bool() == Some(true) {
        return Ok(Application::UnknownOrder);
    }
    let raw_id = entry["raw_ref"]
        .as_str()
        .and_then(|id| Uuid::parse_str(id).ok())
        .ok_or(ReplayInputError::HistoricalContract)?;
    let at = entry["at"]
        .as_str()
        .and_then(|clock| clock.parse().ok())
        .ok_or(ReplayInputError::HistoricalClock)?;
    let timezone = entry["timezone"]
        .as_str()
        .filter(|zone| known_timezone(zone))
        .ok_or(ReplayInputError::HistoricalTimezone)?
        .to_owned();
    let replacement = HealthReplacement::restore(endpoint, &entry["replacement"])
        .map_err(|_| ReplayInputError::HistoricalContract)?;
    Ok(Application::Verified {
        raw_id,
        at,
        timezone,
        replacement,
    })
}

pub(super) async fn read_document(
    txn: &mut Transaction<'_, Postgres>,
    raw_id: Uuid,
    endpoint: &str,
    key: &str,
    archive: Arc<Archive>,
    limit: u64,
    cancelled: impl Fn() -> bool + Send + 'static,
) -> Result<Value, ReplayInputError> {
    let (archive_key, digest): (String, String) = sqlx::query_as(
        "SELECT archive_key, payload_hash FROM source_payloads
         WHERE id=$1 AND source=$2 AND endpoint=$3 AND source_key=$4",
    )
    .bind(raw_id)
    .bind(GARMIN_LEGACY_SOURCE)
    .bind(endpoint)
    .bind(key)
    .fetch_optional(&mut **txn)
    .await?
    .ok_or(ReplayInputError::HistoricalContract)?;
    let archive_key: ArchiveKey = archive_key
        .parse()
        .map_err(|_| ReplayInputError::InvalidArchiveKey)?;
    if archive_key.digest_hex() != digest {
        return Err(ReplayInputError::ArchiveHashMismatch);
    }
    tokio::task::spawn_blocking(move || {
        let file = archive
            .open_verified_reader(&archive_key, limit, cancelled)
            .map_err(ReplayInputError::Archive)?;
        serde_json::from_reader(BufReader::new(file.take(limit.saturating_add(1))))
            .map_err(|_| ReplayInputError::InvalidPayload)
    })
    .await
    .map_err(|_| ReplayInputError::ArchiveTask)?
}
