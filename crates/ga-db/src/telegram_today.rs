use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::Date;
use uuid::Uuid;

use crate::{
    outbox_claims::control_reply_fence,
    replay::{GARMIN_PARSER_VERSION, projection_pending},
};

pub(crate) const REPLAY_NOTICE: &str = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже.";

#[derive(Deserialize)]
pub(crate) struct TodayProjection {
    generation: Value,
}

impl TodayProjection {
    pub(crate) async fn requires_notice(
        &self,
        txn: &mut Transaction<'_, Postgres>,
    ) -> Result<bool, sqlx::Error> {
        let current: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='replay:generation'")
                .fetch_optional(&mut **txn)
                .await?;
        let current = current
            .map(|value| serde_json::from_str::<Value>(&value))
            .transpose()
            .map_err(|error| sqlx::Error::Decode(Box::new(error)))?
            .unwrap_or(Value::Null);
        Ok(self.generation != current || projection_pending(txn, GARMIN_PARSER_VERSION).await?)
    }
}

/// Capture the latest saved daily summary and the generation that made it available.
pub(crate) async fn capture(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Uuid,
) -> Result<(String, String), sqlx::Error> {
    let generation: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='replay:generation'")
            .fetch_optional(&mut **txn)
            .await?;
    let generation = generation
        .map(|value| serde_json::from_str::<Value>(&value))
        .transpose()
        .map_err(|error| sqlx::Error::Decode(Box::new(error)))?;
    let fence = control_reply_fence(
        txn,
        conversation,
        json!({
            "telegram_today": {"generation": generation},
        }),
    )
    .await?;
    if projection_pending(txn, GARMIN_PARSER_VERSION).await? {
        return Ok((REPLAY_NOTICE.into(), fence));
    }
    type Day = (
        Date,
        Option<f64>,
        Option<f64>,
        Option<f64>,
        Option<f64>,
        Option<f64>,
    );
    let day: Option<Day> = sqlx::query_as(
        "SELECT day,sleep_score,hrv_nightly_avg,resting_hr,body_battery_high,training_readiness_score
         FROM health_days ORDER BY day DESC LIMIT 1",
    ).fetch_optional(&mut **txn).await?;
    let Some((day, sleep, hrv, resting, battery, readiness)) = day else {
        return Ok(("Показатели Garmin ещё не загружены.".into(), fence));
    };
    let (year, month, day) = day.to_calendar_date();
    let month = u8::from(month);
    let mut reply = format!("Последние сохранённые показатели за {day:02}.{month:02}.{year:04}:");
    for (label, value) in [
        ("Сон", sleep),
        ("HRV", hrv),
        ("Пульс покоя", resting),
        ("Body Battery максимум", battery),
        ("Готовность", readiness),
    ] {
        let value = value.map_or_else(|| "нет данных".into(), format_number);
        reply.push_str(&format!("\n{label}: {value}"));
    }
    Ok((reply, fence))
}

fn format_number(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    let display = format!("{value:?}");
    let Some((mantissa, exponent)) = display.split_once('e') else {
        return display;
    };
    exponent.parse::<i32>().map_or_else(
        |_| display.clone(),
        |exponent| format!("{mantissa}e{exponent:+03}"),
    )
}
