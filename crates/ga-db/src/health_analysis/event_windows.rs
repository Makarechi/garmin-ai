use std::time::Duration;

use ga_domain::{legacy_metrics::legacy_metric_contracts, model_consent::ModelCategories};
use serde_json::{Value, json};
use time::{OffsetDateTime, UtcOffset};
use uuid::Uuid;

use super::{
    DailyHealthReader, HealthAnalysisError, SeriesReadError, check_calendar_bounds, isoformat,
    series::series_in,
};
use crate::{
    model_gate::onboarding::onboarding_allows_model, scenario_packs::model_disallowed_event_kinds,
    system_definitions::known_system_event,
};

impl DailyHealthReader {
    /// Verify the diary category and selected event pack in the same analytic snapshot.
    pub async fn permits_model_event_kind(
        &mut self,
        kind: &str,
    ) -> Result<bool, HealthAnalysisError> {
        Ok(
            onboarding_allows_model(&mut self.0, ModelCategories::DIARY).await?
                && !model_disallowed_event_kinds(&mut self.0)
                    .await
                    .map_err(|_| HealthAnalysisError::Database)?
                    .contains(&kind),
        )
    }

    /// Read source-separated physiology around confirmed episodes without treating gaps as negatives.
    pub async fn event_windows(
        mut self,
        kind: &str,
        metric: &str,
        start: OffsetDateTime,
        end: OffsetDateTime,
    ) -> Result<Value, SeriesReadError> {
        check_calendar_bounds(
            start.to_offset(UtcOffset::UTC).date(),
            end.to_offset(UtcOffset::UTC).date(),
        )
        .map_err(|_| SeriesReadError::Invalid)?;
        let contract = legacy_metric_contracts()
            .remove(metric)
            .filter(|contract| contract["kind"] != "daily_summary")
            .ok_or(SeriesReadError::Invalid)?;
        if start >= end
            || end - start > time::Duration::days(366)
            || !known_system_event(kind).map_err(|_| HealthAnalysisError::Database)?
        {
            return Err(SeriesReadError::Invalid);
        }
        let work = async {
            let episodes: Vec<(Uuid, OffsetDateTime)> = sqlx::query_as(
                "SELECT id,start FROM events WHERE kind=$1 AND NOT deleted
                 AND status='confirmed' AND start >= $2 AND start < $3
                 ORDER BY start LIMIT 101",
            )
            .bind(kind)
            .bind(start)
            .bind(end)
            .fetch_all(&mut *self.0)
            .await?;
            if episodes.len() > 100 {
                return Err(SeriesReadError::Invalid);
            }
            let mut rows = Vec::with_capacity(episodes.len());
            for (id, at) in episodes {
                let mut windows = Vec::with_capacity(5);
                for (low, high) in [(-48, -24), (-24, -12), (-12, -6), (-6, 0), (0, 24)] {
                    let left = at + time::Duration::hours(low);
                    let right = at + time::Duration::hours(high);
                    let mut series = series_in(
                        &mut self.0,
                        metric,
                        left,
                        right,
                        ((high - low) * 60) as u16,
                        32,
                        left,
                    )
                    .await?;
                    let sources = series["rows"].take();
                    let truncated = series["truncated"].take();
                    let mut window = json!({
                        "relative_hours":[low,high],"start":isoformat(left),"end":isoformat(right),
                        "truncated":truncated,
                    });
                    window["sources"] = sources;
                    windows.push(window);
                }
                let mut episode = json!({"event_id":id.to_string(),"start":isoformat(at)});
                episode["windows"] = Value::Array(windows);
                rows.push(episode);
            }
            self.0.commit().await?;
            let mut result = json!({
                "event_type":kind,"metric":metric,"episodes":rows.len(),"metric_contract":contract,
                "limitations":[
                    "Catalog aggregation applies per source; sparse gauges have unknown means",
                    "No event or no samples is missing evidence, never evidence of absence",
                ],
            });
            result["rows"] = Value::Array(rows);
            Ok(result)
        };
        tokio::time::timeout(Duration::from_secs(15), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}
