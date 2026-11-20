use std::{io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::timestamp::{StoredInstant, python_isoformat_microseconds};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{DailyHealthReader, HealthAnalysisError, isoformat};

#[derive(Default)]
struct Cursor {
    before: Option<(OffsetDateTime, Uuid)>,
    empty: bool,
}

impl DailyHealthReader {
    /// Page retained insight records in stable descending timestamp/identity order.
    pub async fn insights(
        mut self,
        limit: u16,
        cursor: Option<&str>,
    ) -> Result<JsonDocument, HealthAnalysisError> {
        let work = async {
            sqlx::query("SET LOCAL statement_timeout = '120s'")
                .execute(&mut *self.0)
                .await?;
            let result = insights_in(&mut self.0, limit, cursor, None)
                .await?
                .ok_or(HealthAnalysisError::Database)?;
            self.0.commit().await?;
            Ok(result)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

pub(super) async fn insights_in(
    txn: &mut Transaction<'_, Postgres>,
    limit: u16,
    cursor: Option<&str>,
    mut remaining_bytes: Option<usize>,
) -> Result<Option<JsonDocument>, HealthAnalysisError> {
    if !(1..=100).contains(&limit) {
        return Err(HealthAnalysisError::InsightArguments);
    }
    let cursor = cursor.map(parse_cursor).transpose()?.unwrap_or_default();
    let mut document = JsonSpool::default();
    document.write_all(b"{\"rows\":[").map_err(storage_error)?;
    let rows = sqlx::query_as::<_, (Uuid, OffsetDateTime, Option<f64>)>(
        "SELECT id,generated_at,effect_size FROM insights
         WHERE NOT $4 AND ($1::timestamptz IS NULL OR (generated_at,id)<($1,$2::uuid))
         ORDER BY generated_at DESC,id DESC LIMIT $3",
    )
    .bind(cursor.before.map(|(at, _)| at))
    .bind(cursor.before.map(|(_, id)| id))
    .bind(i64::from(limit) + 1)
    .bind(cursor.empty)
    .fetch_all(&mut **txn)
    .await?;
    let truncated = rows.len() > usize::from(limit);
    let mut last = None;
    for (index, (id, at, effect)) in rows.into_iter().take(usize::from(limit)).enumerate() {
        if index != 0 {
            document.write_all(b",").map_err(storage_error)?;
        }
        let at = isoformat(at);
        last = Some(format!("{at}|{id}"));
        let effect = serde_json::to_string(&effect).map_err(|_| HealthAnalysisError::Database)?;
        let mut chunks = sqlx::query_scalar::<_, Vec<u8>>(include_str!("insight_chunks.sql"))
            .bind(id)
            .bind(at)
            .bind(effect)
            .fetch(&mut **txn);
        while let Some(chunk) = chunks.try_next().await? {
            if let Some(remaining) = &mut remaining_bytes {
                let Some(next) = remaining.checked_sub(chunk.len()) else {
                    return Ok(None);
                };
                *remaining = next;
            }
            document.write_all(&chunk).map_err(storage_error)?;
        }
    }
    document
        .write_all(b"],\"truncated\":")
        .map_err(storage_error)?;
    serde_json::to_writer(&mut document, &truncated).map_err(|_| HealthAnalysisError::Database)?;
    document
        .write_all(b",\"next_cursor\":")
        .map_err(storage_error)?;
    let next = if truncated { last } else { None };
    serde_json::to_writer(&mut document, &next).map_err(|_| HealthAnalysisError::Database)?;
    document.write_all(b"}").map_err(storage_error)?;
    document.finish().map(Some).map_err(storage_error)
}

fn parse_cursor(text: &str) -> Result<Cursor, HealthAnalysisError> {
    let invalid = || HealthAnalysisError::InsightArguments;
    if text.chars().count() > 200 {
        return Err(invalid());
    }
    let (at, id) = text.split_once('|').ok_or_else(invalid)?;
    let micros = python_isoformat_microseconds(at).ok_or_else(invalid)?;
    let id = Uuid::parse_str(id).map_err(|_| invalid())?;
    match StoredInstant::from_unix_microseconds(micros) {
        Ok(at) => Ok(Cursor {
            before: Some((at.into(), id)),
            empty: false,
        }),
        Err(_) => Ok(Cursor {
            before: None,
            empty: micros < 0,
        }),
    }
}

fn storage_error(_: std::io::Error) -> HealthAnalysisError {
    HealthAnalysisError::Database
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use ga_domain::timestamp::StoredInstant;
    use time::{OffsetDateTime, UtcOffset, format_description::well_known::Rfc3339};
    use uuid::Uuid;

    use super::{HealthAnalysisError, parse_cursor};

    #[test]
    fn preserves_the_instant_and_identifier_across_time_offsets() -> Result<(), Box<dyn Error>> {
        let anchor: StoredInstant = "2026-10-25T01:30:00Z".parse()?;
        for minutes in [-720, -450, -330, -60, 0, 60, 120, 345, 765, 840] {
            let offset = UtcOffset::from_whole_seconds(minutes * 60)?;
            for micros in [0, 1, 1005, 123_456, 999_999] {
                let instant = OffsetDateTime::from(anchor).replace_microsecond(micros)?;
                let timestamp = instant.to_offset(offset).format(&Rfc3339)?;
                let id = Uuid::from_u128(u128::from(micros) << 64 | 0x1234_5678_9abc_def0);
                for identifier in [
                    id.to_string(),
                    id.simple().to_string(),
                    id.braced().to_string(),
                    id.urn().to_string(),
                ] {
                    let cursor = parse_cursor(&format!("{timestamp}|{identifier}"))?;
                    assert_eq!(cursor.before, Some((instant, id)));
                    assert!(!cursor.empty);
                }
            }
        }
        Ok(())
    }

    #[test]
    fn rejects_broken_positions_and_bounds_out_of_range_instants() -> Result<(), Box<dyn Error>> {
        let id = Uuid::from_u128(17);
        let timestamp = "2026-10-25T01:30:00.000001Z";
        for input in [
            format!("{timestamp}¦{id}"),
            format!("{timestamp}||{id}"),
            format!("{timestamp}|{id}|"),
            format!("{timestamp}|{id}é"),
            format!("2026-02-29T00:00:00Z|{id}"),
            format!("2026-10-25T24:00:01Z|{id}"),
            format!("{timestamp}|{}", "中".repeat(201)),
        ] {
            assert!(
                matches!(
                    parse_cursor(&input),
                    Err(HealthAnalysisError::InsightArguments)
                ),
                "{input:?}"
            );
        }
        let midnight: StoredInstant = "2026-10-26T00:00:00Z".parse()?;
        let rollover = parse_cursor(&format!("2026-10-25T24:00:00Z|{id}"))?;
        assert_eq!(rollover.before, Some((midnight.into(), id)));
        for (timestamp, empty) in [
            ("0001-01-01T00:00:00+01:00", true),
            ("9999-12-31T23:59:59-01:00", false),
        ] {
            let cursor = parse_cursor(&format!("{timestamp}|{id}"))?;
            assert!(cursor.before.is_none());
            assert_eq!(cursor.empty, empty);
        }
        Ok(())
    }
}
