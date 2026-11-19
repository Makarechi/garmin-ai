use std::time::Duration;

use ga_domain::{
    event_input::source_offset_datetime,
    legacy_json::{self, Utf8Layout},
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use super::{HealthAnalysisError, HealthPeriod, owner_timezone, period_comparison};
use crate::{
    Database,
    activity_documents::PARSER_VERSION,
    check_readiness,
    garmin_integration::garmin_pause,
    replay::{REPLAY_MUTATION_LOCK, projection_pending},
    share_policy::OWNER_ONBOARDING_LOCK,
};

const SLOT: &str = "insights:native-generation-slot";
const METRICS: [(&str, &str, &str); 5] = [
    ("sleep_score", "sleep", "Оценка сна"),
    ("sleep_seconds", "sleep", "Продолжительность сна (секунды)"),
    ("hrv_nightly_avg", "wellbeing", "Ночной HRV (мс)"),
    ("resting_hr", "wellbeing", "Пульс покоя (уд/мин)"),
    ("stress_avg", "wellbeing", "Средний стресс"),
];

impl Database {
    /// Generate existing trend records at most once per six-hour interval.
    /// A busy source/replay fence leaves the same interval eligible for retry.
    pub async fn generate_trend_insights(
        &self,
        at: StoredInstant,
        configured_timezone: &str,
    ) -> Result<bool, HealthAnalysisError> {
        tokio::time::timeout(Duration::from_secs(5), async {
            let mut txn = self.work.begin().await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'")
                .execute(&mut *txn)
                .await?;
            check_readiness(&mut txn)
                .await
                .map_err(HealthAnalysisError::Storage)?;
            let slot = OffsetDateTime::from(at).unix_timestamp().div_euclid(21_600);
            if previous_slot(&mut txn)
                .await?
                .is_some_and(|previous| slot <= previous)
            {
                return Ok(true);
            }
            for fence in [REPLAY_MUTATION_LOCK, OWNER_ONBOARDING_LOCK] {
                let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                    .bind(fence)
                    .fetch_one(&mut *txn)
                    .await?;
                if !locked {
                    return Ok(false);
                }
            }
            if previous_slot(&mut txn)
                .await?
                .is_some_and(|previous| slot <= previous)
            {
                return Ok(true);
            }
            let has_health: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM people WHERE singleton)
                     AND EXISTS (SELECT 1 FROM health_days)",
            )
            .fetch_one(&mut *txn)
            .await?;
            if !has_health {
                return Ok(true);
            }
            if is_withheld(&mut txn, at).await? {
                finish_slot(&mut txn, slot).await?;
                txn.commit().await?;
                return Ok(true);
            }
            let sync_busy: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM jobs
                    WHERE kind IN ('garmin_endpoint', 'garmin_activities', 'garmin_fit')
                    AND status IN ('pending','running'))",
            )
            .fetch_one(&mut *txn)
            .await?;
            if sync_busy || projection_pending(&mut txn, PARSER_VERSION).await? {
                return Ok(false);
            }
            let timezone = owner_timezone(&mut txn, configured_timezone).await?;
            let today = source_offset_datetime(at, &timezone)
                .ok_or(HealthAnalysisError::DateRange)?
                .date();
            generate(&mut txn, at, today).await?;
            finish_slot(&mut txn, slot).await?;
            txn.commit().await?;
            Ok(true)
        })
        .await
        .map_err(|_| HealthAnalysisError::Database)?
    }
}

async fn previous_slot(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Option<i64>, HealthAnalysisError> {
    let stored: Option<String> =
        sqlx::query_scalar("SELECT value->>'slot' FROM app_state WHERE key=$1")
            .bind(SLOT)
            .fetch_optional(&mut **txn)
            .await?
            .flatten();
    stored
        .map(|slot| slot.parse().map_err(|_| HealthAnalysisError::Database))
        .transpose()
}

async fn finish_slot(txn: &mut Transaction<'_, Postgres>, slot: i64) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES ($1,jsonb_build_object('slot',$2::bigint))
         ON CONFLICT (key) DO UPDATE SET value=excluded.value",
    )
    .bind(SLOT)
    .bind(slot)
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn is_withheld(
    txn: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
) -> Result<bool, HealthAnalysisError> {
    let withheld = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS (SELECT 1 FROM app_state
                        WHERE key='proactive:enabled' AND value->'enabled'='false')",
    )
    .fetch_one(&mut **txn)
    .await?;
    Ok(withheld
        || garmin_pause(&mut **txn, at)
            .await
            .map_err(|_| HealthAnalysisError::Database)?
            .is_some())
}

async fn generate(
    txn: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
    today: Date,
) -> Result<(), HealthAnalysisError> {
    let (year, week, _) = today.to_iso_week_date();
    let days = time::Duration::days;
    let first = HealthPeriod::new(today - days(14), today - days(1))?;
    let second = HealthPeriod::new(today - days(28), today - days(15))?;
    for (metric, pack, label) in METRICS {
        let key = format!("trend:{metric}:{year}:{week}");
        if !update_allowed(txn, at, metric, pack, &key).await? {
            continue;
        }
        let result = period_comparison(txn, metric, first, second)
            .await?
            .document();
        let (a, b) = (&result["a"], &result["b"]);
        let sample = |window: &Value| window["n"].as_i64().unwrap_or_default();
        let effect = result["standardized_difference"].as_f64();
        let difference = result["difference"].as_f64();
        let consistent = result["ci95"][0]
            .as_f64()
            .zip(result["ci95"][1].as_f64())
            .is_some_and(|(low, high)| low * high > 0.0);
        let constant_shift = a["sd"].as_f64() == Some(0.0)
            && b["sd"].as_f64() == Some(0.0)
            && difference.is_some_and(|value| value != 0.0);
        let accepted = sample(a) >= 14
            && sample(b) >= 14
            && consistent
            && (constant_shift || effect.is_some_and(|value| value.abs() >= 0.5));
        let statement = format!(
            "{label}: сравнение {}–{} и {}–{}; разница {}; наблюдений {} и {}, интервал 95%: {}. Связь не доказывает причину.",
            text(&a["start"]),
            text(&a["end"]),
            text(&b["start"]),
            text(&b["end"]),
            number_display(&result["difference"])?,
            sample(a),
            sample(b),
            number_display(&result["ci95"])?
        );
        sqlx::query(
            "INSERT INTO insights(id,category,statement,evidence,sample_size,effect_size,status,dedup_key)
               VALUES ($1,'trend',$2,$3::jsonb,$4,$5,$6,$7)
             ON CONFLICT (dedup_key) DO UPDATE SET statement=excluded.statement,
               evidence=excluded.evidence,sample_size=excluded.sample_size,
               effect_size=excluded.effect_size,status=excluded.status,generated_at=$8",
        )
        .bind(Uuid::new_v4())
        .bind(statement)
        .bind(result.to_string())
        .bind(i32::try_from(sample(a) + sample(b)).map_err(|_| HealthAnalysisError::Database)?)
        .bind(effect)
        .bind(if accepted { "accepted" } else { "candidate" })
        .bind(key)
        .bind(OffsetDateTime::from(at))
        .execute(&mut **txn)
        .await?;
    }
    Ok(())
}

async fn update_allowed(
    txn: &mut Transaction<'_, Postgres>,
    at: StoredInstant,
    metric: &str,
    pack: &str,
    key: &str,
) -> Result<bool, HealthAnalysisError> {
    let enabled: bool = sqlx::query_scalar(
        "SELECT (NOT EXISTS (
                    SELECT 1 FROM module_configs m JOIN people p ON p.id=m.owner_id WHERE p.singleton
                 ) OR EXISTS (
                    SELECT 1 FROM module_configs m JOIN people p ON p.id=m.owner_id
                    WHERE p.singleton AND m.pack_key=$1 AND m.reminders_enabled
                 ))
                 AND NOT EXISTS (
                    SELECT 1 FROM insights WHERE dedup_key=$2 AND
                         (status IN ('delivered','uncertain') OR
                          (status='cancelled' AND evidence->>'cancel_reason'='owner_pause'))
                 )",
    )
    .bind(pack)
    .bind(key)
    .fetch_one(&mut **txn)
    .await?;
    if !enabled {
        return Ok(false);
    }
    let last: Option<Option<String>> =
        sqlx::query_scalar("SELECT value->>'at' FROM app_state WHERE key=$1")
            .bind(format!("insight:last:{metric}"))
            .fetch_optional(&mut **txn)
            .await?;
    let Some(last) = last else {
        return Ok(true);
    };
    let last = last
        .and_then(|last| last.parse::<StoredInstant>().ok())
        .ok_or(HealthAnalysisError::Database)?;
    Ok(OffsetDateTime::from(last) <= OffsetDateTime::from(at) - time::Duration::days(7))
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap_or_default()
}

fn number_display(value: &Value) -> Result<String, HealthAnalysisError> {
    if value.is_null() {
        return Ok("None".to_owned());
    }
    legacy_json::encode_ascii(value.to_string().as_bytes(), Utf8Layout::Archive, 1024)
        .map_err(|_| HealthAnalysisError::Database)
}
