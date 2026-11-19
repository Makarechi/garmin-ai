use std::{io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::analysis_spec::AnalysisSpec;
use sqlx_postgres::types::PgInterval;
use uuid::Uuid;

use super::{GenericAnalysisError, GenericAnalysisReader};

struct SourceSelection {
    mode: &'static str,
    account: Option<String>,
    device: Option<String>,
    measurement: Option<String>,
}

impl SourceSelection {
    fn from_spec(spec: &AnalysisSpec) -> Result<Self, GenericAnalysisError> {
        let mut selection = Self {
            mode: "all",
            account: None,
            device: None,
            measurement: None,
        };
        if let Some(source) = spec.document()["source"].as_str() {
            if let Some(value) = source.strip_prefix("measurement:") {
                selection.mode = "measurement";
                selection.measurement = Some(value.to_owned());
            } else if let Some(value) = source.strip_prefix("observation:") {
                selection.mode = "observation";
                let [account, device]: [Option<String>; 2] =
                    serde_json::from_str(value).map_err(|_| GenericAnalysisError::Encoding)?;
                selection.account = account;
                selection.device = device;
            } else {
                selection.mode = "event";
            }
        }
        Ok(selection)
    }
}

impl GenericAnalysisReader {
    async fn metric_contract(&mut self, spec: &AnalysisSpec) -> Result<Uuid, GenericAnalysisError> {
        let definition: Option<(Uuid, i32)> =
            sqlx::query_as("SELECT id,current_version FROM metric_definitions WHERE key=$1")
                .bind(spec.document()["metric_key"].as_str())
                .fetch_optional(&mut *self.txn)
                .await?;
        let (definition, current_version) =
            definition.ok_or(GenericAnalysisError::Lookup("Metric definition not found"))?;
        let requested = &spec.document()["metric_version"];
        let version = if requested.is_null() {
            current_version
        } else {
            requested
                .as_i64()
                .and_then(|value| i32::try_from(value).ok())
                .ok_or(GenericAnalysisError::Data)?
        };
        sqlx::query_scalar(
            "SELECT id FROM metric_definition_versions WHERE definition_id=$1 AND version=$2",
        )
        .bind(definition)
        .bind(version)
        .fetch_optional(&mut *self.txn)
        .await?
        .ok_or(GenericAnalysisError::Lookup("Metric version not found"))
    }

    /// Read only observations and measurement revisions known at the requested cutoff.
    pub async fn query_observations(
        mut self,
        spec: &AnalysisSpec,
    ) -> Result<JsonDocument, GenericAnalysisError> {
        let work = async {
            self.prepare(spec).await?;
            let contract = self.metric_contract(spec).await?;
            let source = SourceSelection::from_spec(spec)?;
            self.restore_display_timezone().await?;
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
            let mut chunks =
                sqlx::query_as::<_, (bool, i64, Vec<u8>)>(include_str!("observations.sql"))
                    .bind(contract)
                    .bind(spec.start())
                    .bind(spec.end())
                    .bind(PgInterval {
                        months: 0,
                        days: 0,
                        microseconds: spec.knowledge_cutoff_microseconds(),
                    })
                    .bind(limit)
                    .bind(source.mode)
                    .bind(source.account)
                    .bind(source.device)
                    .bind(source.measurement)
                    .bind(spec.hash())
                    .bind(spec.document()["metric_key"].as_str())
                    .bind(cutoff)
                    .fetch(&mut *self.txn);
            let mut output = JsonSpool::default();
            while let Some((invalid_snapshot, count, bytes)) = chunks.try_next().await? {
                if invalid_snapshot {
                    return Err(GenericAnalysisError::InvalidSnapshot);
                }
                if count > limit {
                    return Err(GenericAnalysisError::Value(
                        "Observation query exceeds its explicit result limit",
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
