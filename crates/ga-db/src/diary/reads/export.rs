use futures_util::TryStreamExt;
use ga_domain::{
    event_input::{known_timezone, source_offset_datetime},
    legacy_json::{self, Utf8Layout},
    timestamp::{OffsetTimestamp, StoredInstant},
};
use serde_json::{Map, Value, json};
use time::{Duration, OffsetDateTime};

use super::{DiaryReadError, EventRow, RANGE_EVENTS, check_range, serialize};
use crate::{Database, health_analysis::owner_timezone};

const ROW_LIMIT: usize = 1000;
const SIZE_LIMIT: usize = 2_000_000;
const EXPORT_FIELDS: &[&str] = &[
    "id",
    "kind",
    "start",
    "end",
    "timezone",
    "source",
    "status",
    "confidence",
    "revision",
    "definition_version_id",
    "payload",
    "topology",
    "missing_end",
];

impl Database {
    /// Export an owner's diary in the same read snapshot as its display settings.
    pub async fn export_diary(
        &self,
        start: StoredInstant,
        end: StoredInstant,
        timezone: Option<&str>,
        configured_timezone: &str,
    ) -> Result<Value, DiaryReadError> {
        let start_at = OffsetDateTime::from(start);
        let end_at = OffsetDateTime::from(end);
        check_range(start_at, end_at)?;
        if end_at - start_at > Duration::days(31) {
            return Err(DiaryReadError::TimestampRange);
        }
        tokio::time::timeout(std::time::Duration::from_secs(4), async {
            let mut txn = self.begin_diary_read().await?;
            let zone = match timezone.filter(|value| !value.is_empty()) {
                Some(zone) => zone.to_owned(),
                None => owner_timezone(&mut txn, configured_timezone)
                    .await
                    .map_err(|_| DiaryReadError::Database)?,
            };
            if !known_timezone(&zone) {
                return Err(DiaryReadError::InvalidZone);
            }
            let mut result = json!({
                "format_version":1,"start":start.to_string(),"end":end.to_string(),
                "display_timezone":zone,
                "selection":"overlap_half_open; original intervals are not clipped",
                "missingness":"unreported_is_unknown","rows":[],
            });
            let mut size = python_size(&result)?;
            let mut rows = Vec::new();
            let mut entries = sqlx::query_as::<_, EventRow>(RANGE_EVENTS)
                .bind(start_at)
                .bind(end_at)
                .bind(Option::<&str>::None)
                .bind((ROW_LIMIT + 1) as i64)
                .bind(Option::<&str>::None)
                .bind(Option::<&str>::None)
                .bind(&[] as &[&str])
                .fetch(&mut *txn);
            while let Some((
                document,
                start,
                end,
                created,
                updated,
                recorded,
                ingested,
                can_update,
            )) = entries.try_next().await?
            {
                if rows.len() == ROW_LIMIT {
                    return Err(DiaryReadError::Limit);
                }
                let row = serialize(
                    (document, start, end, created, updated, recorded, ingested),
                    can_update,
                )?;
                let mut entry: Map<String, Value> = EXPORT_FIELDS
                    .iter()
                    .map(|&field| (field.to_owned(), row[field].clone()))
                    .collect();
                entry.insert("display_start".into(), json!(display(start, &zone)?));
                entry.insert(
                    "display_end".into(),
                    json!(end.map(|end| display(end, &zone)).transpose()?),
                );
                let entry = Value::Object(entry);
                let separator = usize::from(!rows.is_empty()) * 2;
                size = size
                    .saturating_add(python_size(&entry)?)
                    .saturating_add(separator);
                if size > SIZE_LIMIT {
                    return Err(DiaryReadError::Limit);
                }
                rows.push(entry);
            }
            drop(entries);
            result["rows"] = Value::Array(rows);
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }
}

fn display(value: OffsetDateTime, zone: &str) -> Result<String, DiaryReadError> {
    let at = StoredInstant::try_from(value).map_err(|_| DiaryReadError::InvalidEvent)?;
    let local = source_offset_datetime(at, zone).ok_or(DiaryReadError::InvalidZone)?;
    Ok(OffsetTimestamp(local).to_string())
}

fn python_size(value: &Value) -> Result<usize, DiaryReadError> {
    let bytes = serde_json::to_vec(value).map_err(|_| DiaryReadError::InvalidEvent)?;
    let formatted = legacy_json::encode(&bytes, Utf8Layout::Archive, SIZE_LIMIT)
        .map_err(|_| DiaryReadError::Limit)?;
    Ok(formatted.len())
}
