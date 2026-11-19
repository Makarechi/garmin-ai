use std::{io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::analysis_spec::AnalysisSpec;
use sqlx_postgres::types::PgInterval;
use uuid::Uuid;

use super::{GenericAnalysisError, GenericAnalysisReader};

impl GenericAnalysisReader {
    /// Reconstruct each entry from its last known audit or first future preimage.
    pub async fn query_entries(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let key = spec.document()["definition_key"]
                .as_str()
                .ok_or(GenericAnalysisError::Lookup("Event definition not found"))?;
            let definition: Option<Uuid> =
                sqlx::query_scalar("SELECT id FROM event_definitions WHERE key=$1")
                    .bind(key)
                    .fetch_optional(&mut *self.txn)
                    .await?;
            let definition =
                definition.ok_or(GenericAnalysisError::Lookup("Event definition not found"))?;
            let relation = spec.document()["time_relation"]
                .as_str()
                .ok_or(GenericAnalysisError::Database)?;
            let candidates: Vec<Uuid> = sqlx::query_scalar(include_str!("entries_candidates.sql"))
                .bind(definition)
                .bind(spec.start())
                .bind(spec.end())
                .bind(relation)
                .fetch_all(&mut *self.txn)
                .await?;
            if candidates.len() > 10000 {
                return Err(GenericAnalysisError::Value(
                    "Entry history exceeds its bounded reconstruction limit",
                ));
            }
            let limit = spec.document()["limit"]
                .as_i64()
                .ok_or(GenericAnalysisError::Database)?;
            let cutoff = spec.document()["knowledge_cutoff"]
                .as_str()
                .ok_or(GenericAnalysisError::Database)?;
            let cutoff = match cutoff.strip_suffix('Z') {
                Some(value) => format!("{value}+00:00"),
                None => cutoff.to_owned(),
            };
            self.restore_display_timezone().await?;
            let mut chunks =
                sqlx::query_as::<_, (bool, i64, Vec<u8>)>(include_str!("entries_snapshots.sql"))
                    .bind(candidates)
                    .bind(PgInterval {
                        months: 0,
                        days: 0,
                        microseconds: spec.knowledge_cutoff_microseconds(),
                    })
                    .bind(definition)
                    .bind(spec.start())
                    .bind(spec.end())
                    .bind(relation)
                    .bind(limit)
                    .bind(spec.hash())
                    .bind(key)
                    .bind(cutoff)
                    .fetch(&mut *self.txn);
            let mut output = JsonSpool::default();
            while let Some((unavailable, count, bytes)) = chunks.try_next().await? {
                if unavailable {
                    return Err(GenericAnalysisError::Value(
                        "Historical entry state is unavailable at this cutoff",
                    ));
                }
                if count > limit {
                    return Err(GenericAnalysisError::Value(
                        "Entry query exceeds its explicit result limit",
                    ));
                }
                output
                    .write_all(&bytes)
                    .map_err(|_| GenericAnalysisError::Database)?;
            }
            drop(chunks);
            self.txn.commit().await?;
            output.finish().map_err(|_| GenericAnalysisError::Database)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| GenericAnalysisError::Database)?
    }
}
