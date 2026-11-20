use std::{collections::BTreeMap, time::Duration};

use ga_domain::{
    event_input::{known_timezone, local_midnight},
    statistics::{describe, match_weekday_controls, paired_mean_difference},
};
use serde_json::{Value, json};
use sqlx::{Postgres, QueryBuilder, Transaction};
use time::{Date, OffsetDateTime};

use super::{DailyHealthReader, HealthAnalysisError, HealthPeriod, daily_metric, owner_timezone};

impl DailyHealthReader {
    /// Compare logged migraine days with explicitly observed, full-day negative controls.
    pub async fn migraine_comparison(
        mut self,
        metric: &str,
        period: HealthPeriod,
        requested_timezone: Option<&str>,
        configured_timezone: &str,
    ) -> Result<Value, HealthAnalysisError> {
        let work = async {
            sqlx::query("SET LOCAL statement_timeout = '15s'")
                .execute(&mut *self.0)
                .await?;
            let value = migraine_comparison_in(
                &mut self.0,
                metric,
                period,
                requested_timezone,
                configured_timezone,
            )
            .await?;
            self.0.commit().await?;
            Ok(value)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
    }
}

pub(super) async fn migraine_comparison_in(
    txn: &mut Transaction<'_, Postgres>,
    metric: &str,
    period: HealthPeriod,
    requested_timezone: Option<&str>,
    configured_timezone: &str,
) -> Result<Value, HealthAnalysisError> {
    let (column, _) = daily_metric(metric)?;
    let timezone = match requested_timezone.filter(|zone| !zone.is_empty()) {
        Some(timezone) => timezone.to_owned(),
        None => owner_timezone(txn, configured_timezone).await?,
    };
    if !known_timezone(&timezone) {
        return Err(HealthAnalysisError::InvalidTimezone);
    }
    let midnight = |day| {
        local_midnight(day, &timezone)
            .map(OffsetDateTime::from)
            .ok_or(HealthAnalysisError::InvalidTimezone)
    };
    let first = period.start - time::Duration::days(56);
    let last = period.end + time::Duration::days(56);
    let episode_left = midnight(period.start - time::Duration::days(59))?;
    let episode_right = midnight(period.end + time::Duration::days(60))?;
    let observation_left = midnight(first)?;
    let observation_right = midnight(last + time::Duration::days(1))?;

    // The interpolated identifier comes exclusively from the daily metric allowlist.
    let mut query = QueryBuilder::<Postgres>::new("SELECT day,");
    query
        .push(column)
        .push("::double precision FROM health_days WHERE day BETWEEN ")
        .push_bind(first)
        .push(" AND ")
        .push_bind(last)
        .push(" ORDER BY day");
    let values: Vec<(Date, Option<f64>)> = query.build_query_as().fetch_all(&mut **txn).await?;
    let values: BTreeMap<_, _> = values.into_iter().collect();
    let days: Vec<_> = values.keys().copied().collect();
    let starts = days
        .iter()
        .map(|day| midnight(*day))
        .collect::<Result<Vec<_>, _>>()?;
    let ends = days
        .iter()
        .map(|day| midnight(*day + time::Duration::days(1)))
        .collect::<Result<Vec<_>, _>>()?;

    let (episodes, episode_start_days): (i64, i64) = sqlx::query_as(
        "SELECT count(*) FILTER(WHERE (start AT TIME ZONE $1)::date BETWEEN $4 AND $5),
            count(DISTINCT (start AT TIME ZONE $1)::date)
                FILTER(WHERE (start AT TIME ZONE $1)::date BETWEEN $4 AND $5)
         FROM events WHERE kind='migraine' AND NOT deleted AND status='confirmed'
            AND start < $3 AND (\"end\" > $2 OR (\"end\" IS NULL AND topology='open_interval')
            OR ((\"end\" IS NULL OR \"end\"=start) AND topology IN ('point','flexible') AND start >= $2))",
    )
    .bind(&timezone).bind(episode_left).bind(episode_right)
    .bind(period.start).bind(period.end).fetch_one(&mut **txn).await?;

    let coverage: Vec<(Date, String, bool, bool)> =
        sqlx::query_as(include_str!("migraine_days.sql"))
            .bind(&timezone)
            .bind(episode_left)
            .bind(episode_right)
            .bind(observation_left)
            .bind(observation_right)
            .bind(&days)
            .bind(&starts)
            .bind(&ends)
            .fetch_all(&mut **txn)
            .await?;

    let mut observed = Vec::new();
    let mut controls = Vec::new();
    let mut control_days = Vec::with_capacity(50);
    let mut eligibility_counts = BTreeMap::<&str, usize>::new();
    let mut coverage_counts = BTreeMap::<String, usize>::new();
    for (day, coverage, excluded, episode_day) in coverage {
        let recorded = values[&day].is_some();
        if episode_day && recorded && period.start <= day && day <= period.end {
            observed.push(day);
        }
        let eligibility = if excluded {
            "episode_exclusion"
        } else if !recorded {
            "missing_metric"
        } else if coverage == "confirmed_negative" {
            controls.push(day);
            "confirmed_control"
        } else if coverage == "positive" {
            "positive_observation"
        } else {
            "exploratory_candidate"
        };
        if control_days.len() < 50 {
            control_days
                .push(json!({"day":day.to_string(),"coverage":coverage,"eligibility":eligibility}));
        }
        *eligibility_counts.entry(eligibility).or_default() += 1;
        *coverage_counts.entry(coverage).or_default() += 1;
    }
    if observed.len() > 200 {
        return Err(HealthAnalysisError::MigraineEpisodeLimit);
    }
    let mut differences = Vec::with_capacity(observed.len());
    let mut pairs = Vec::with_capacity(50);
    for (episode, control) in match_weekday_controls(&observed, &controls) {
        let episode = observed[episode];
        let control = controls[control];
        // Both lists contain only days with recorded values in this snapshot.
        let event_value = values[&episode].ok_or(HealthAnalysisError::Database)?;
        let control_value = values[&control].ok_or(HealthAnalysisError::Database)?;
        differences.push(event_value - control_value);
        if pairs.len() < 50 {
            pairs.push(
                json!({"event_day":episode.to_string(),"control_day":control.to_string(),
                "event_value":event_value,"control_value":control_value}),
            );
        }
    }
    let description = describe(differences.iter().copied().map(Some));
    let intervals = paired_mean_difference(&differences);
    Ok(json!({
        "metric":metric,"algorithm_version":"migraine-controls-v2",
        "status":if differences.len() >= 10 {"descriptive"} else {"insufficient_evidence"},
        "episodes":episodes,"episode_start_days":episode_start_days,
        "control_days":control_days,"control_days_total":days.len(),"control_days_truncated":days.len()>50,
        "control_eligibility_counts":eligibility_counts,"control_coverage_counts":coverage_counts,
        "matched_pairs":differences.len(),"pairs":pairs,"pairs_truncated":differences.len()>50,
        "difference":{"n":description.n,"mean":description.mean,"median":description.median,
            "sd":description.sd,"min":description.min,"max":description.max},
        "ci95":intervals.ci95,"exploratory_sign_permutation_p":intervals.sign_permutation_p,
        "method":"maximum-cardinality same-weekday control matching within 56 days, then minimum total distance; confirmed full-day negative observations only; exclude whole episodes with 3-day pre/post windows; no control reuse",
        "limitations":[
            "Unclosed episodes exclude subsequent controls; missing logs are not negative observations",
            "Unadjusted for medication, sleep, training, alcohol, weather, or other confounders",
            "Intervals require at least 10 pairs; serial dependence and multiple testing limit inference",
            "Association only; cannot establish causes or treatment effects",
        ],
    }))
}
