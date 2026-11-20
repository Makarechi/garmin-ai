use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use super::measurements::MeasurementQuery;
use crate::health_analysis::generic::{GenericAnalysisError, GenericAnalysisReader};

impl GenericAnalysisReader<'_> {
    pub(in crate::health_analysis::generic) async fn aggregate_revisions_current(
        &mut self,
        metric: &str,
        version: i64,
        revisions: &Map<String, Value>,
        now: StoredInstant,
    ) -> Result<bool, GenericAnalysisError> {
        let contract = self
            .metric_contract(&json!({"metric_key":metric,"metric_version":version}))
            .await?;
        self.restore_display_timezone().await?;
        for (reference, revision) in revisions {
            if let Some(encoded) = reference.strip_prefix("measurement:") {
                let Ok((observed, metric, source)) =
                    serde_json::from_str::<(String, String, String)>(encoded)
                else {
                    return Ok(false);
                };
                let Some(observed) = StoredInstant::from_python_isoformat(&observed) else {
                    return Ok(false);
                };
                let start = observed.unix_microseconds();
                let current = self
                    .aggregate_measurement_snapshot(MeasurementQuery {
                        contract,
                        start: Some(start),
                        end: start + 1,
                        cutoff: now.unix_microseconds(),
                        source: Some(&source),
                        limit: i64::MAX,
                        descending: false,
                        include_invalid: true,
                        retain_evidence: true,
                    })
                    .await?;
                let matching = current
                    .into_iter()
                    .find(|row| {
                        row.observed == start && row.metric == metric && row.source == source
                    })
                    .and_then(|row| row.evidence);
                if !matching.is_some_and(|(key, token)| key == *reference && revision == &token) {
                    return Ok(false);
                }
                continue;
            }
            let Ok(event) = Uuid::parse_str(reference) else {
                return Ok(false);
            };
            let current: Option<Option<i32>> = sqlx::query_scalar(
                "SELECT (SELECT max(projection_version) FROM metric_observations
                          WHERE source_entry_id=$1 AND metric_definition_version_id=$2 AND valid)
                 WHERE EXISTS (SELECT FROM events WHERE id=$1 AND NOT deleted)",
            )
            .bind(event)
            .bind(contract)
            .fetch_optional(&mut *self.txn)
            .await?;
            if !current.flatten().is_some_and(|version| revision == version) {
                return Ok(false);
            }
        }
        Ok(true)
    }
}
