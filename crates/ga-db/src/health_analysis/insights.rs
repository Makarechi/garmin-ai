use std::{io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::timestamp::{StoredInstant, python_isoformat_microseconds};
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
        if !(1..=100).contains(&limit) {
            return Err(HealthAnalysisError::InsightArguments);
        }
        let cursor = cursor.map(parse_cursor).transpose()?.unwrap_or_default();
        let work = async {
            sqlx::query("SET LOCAL statement_timeout = '120s'")
                .execute(&mut *self.0)
                .await?;
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
            .fetch_all(&mut *self.0)
            .await?;
            let truncated = rows.len() > usize::from(limit);
            let mut last = None;
            for (index, (id, at, effect)) in rows.into_iter().take(usize::from(limit)).enumerate() {
                if index != 0 {
                    document.write_all(b",").map_err(storage_error)?;
                }
                let at = isoformat(at);
                last = Some(format!("{at}|{id}"));
                let effect =
                    serde_json::to_string(&effect).map_err(|_| HealthAnalysisError::Database)?;
                let mut chunks =
                    sqlx::query_scalar::<_, Vec<u8>>(include_str!("insight_chunks.sql"))
                        .bind(id)
                        .bind(at)
                        .bind(effect)
                        .fetch(&mut *self.0);
                while let Some(chunk) = chunks.try_next().await? {
                    document.write_all(&chunk).map_err(storage_error)?;
                }
            }
            self.0.commit().await?;
            document
                .write_all(b"],\"truncated\":")
                .map_err(storage_error)?;
            serde_json::to_writer(&mut document, &truncated)
                .map_err(|_| HealthAnalysisError::Database)?;
            document
                .write_all(b",\"next_cursor\":")
                .map_err(storage_error)?;
            let next = if truncated { last } else { None };
            serde_json::to_writer(&mut document, &next)
                .map_err(|_| HealthAnalysisError::Database)?;
            document.write_all(b"}").map_err(storage_error)?;
            document.finish().map_err(storage_error)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
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
