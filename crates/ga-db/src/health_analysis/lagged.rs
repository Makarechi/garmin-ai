use std::{collections::BTreeMap, time::Duration};

use ga_domain::statistics::pearson;
use serde_json::{Value, json};
use sqlx::{Postgres, QueryBuilder, Transaction};
use time::Date;

use super::{DailyHealthReader, HealthAnalysisError, HealthPeriod, daily_metric};

impl DailyHealthReader {
    /// Pair recorded calendar days at each requested lag, retaining absent observations as gaps.
    pub async fn lagged_association(
        mut self,
        metric_a: &str,
        metric_b: &str,
        period: HealthPeriod,
        lags: &[i64],
    ) -> Result<Value, HealthAnalysisError> {
        let work = async {
            let value =
                lagged_association_in(&mut self.0, metric_a, metric_b, period, lags).await?;
            self.0.commit().await?;
            Ok(value)
        };
        tokio::time::timeout(Duration::from_millis(1800), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

pub(super) async fn lagged_association_in(
    txn: &mut Transaction<'_, Postgres>,
    metric_a: &str,
    metric_b: &str,
    period: HealthPeriod,
    lags: &[i64],
) -> Result<Value, HealthAnalysisError> {
    let (column_a, _) = daily_metric(metric_a)?;
    let (column_b, _) = daily_metric(metric_b)?;
    if lags.len() > 15 || lags.iter().any(|lag| !(-30..=30).contains(lag)) {
        return Err(HealthAnalysisError::DateRange);
    }
    // Both column names originate from the installed daily-metric allowlist.
    let mut query = QueryBuilder::<Postgres>::new("SELECT day,");
    query
        .push(column_a)
        .push("::double precision,")
        .push(column_b)
        .push("::double precision FROM health_days WHERE day BETWEEN ")
        .push_bind(period.start)
        .push(" AND ")
        .push_bind(period.end)
        .push(" ORDER BY day LIMIT ")
        .push_bind(period.days());
    let observed: Vec<(Date, Option<f64>, Option<f64>)> =
        query.build_query_as().fetch_all(&mut **txn).await?;
    let observed: BTreeMap<_, _> = observed
        .into_iter()
        .map(|(date, a, b)| (date, (a, b)))
        .collect();
    let mut results = Vec::with_capacity(lags.len());
    for &lag in lags {
        let (first, second): (Vec<_>, Vec<_>) = observed
            .iter()
            .filter_map(|(&date, &(a, _))| {
                let next = date.checked_add(time::Duration::days(lag))?;
                Some((a?, observed.get(&next)?.1?))
            })
            .unzip();
        results.push(json!({
            "lag_days":lag,"n":first.len(),"pearson_r":pearson(&first,&second),
        }));
    }
    Ok(json!({
        "a":metric_a,"b":metric_b,"results":results,
        "limitations":[
            "Exploratory correlations; multiple comparisons and serial dependence are not adjusted",
            "A lag does not establish causation",
        ],
    }))
}
