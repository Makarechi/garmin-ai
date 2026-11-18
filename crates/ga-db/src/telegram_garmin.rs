use ga_domain::{event_input::source_calendar_wall_time, timestamp::StoredInstant};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::{Date, OffsetDateTime};
use uuid::Uuid;

use crate::{
    health_analysis::{data_freshness_in, owner_timezone},
    outbox_claims::control_reply_fence,
    replay::{GARMIN_PARSER_VERSION, projection_pending},
};

pub(crate) const REPLAY_NOTICE: &str = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже.";

#[derive(Deserialize)]
pub(crate) struct GarminProjection {
    generation: Value,
}

impl GarminProjection {
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

async fn projection_fence(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Uuid,
    key: &str,
) -> Result<String, sqlx::Error> {
    let generation: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='replay:generation'")
            .fetch_optional(&mut **txn)
            .await?;
    let generation = generation
        .map(|value| serde_json::from_str::<Value>(&value))
        .transpose()
        .map_err(|error| sqlx::Error::Decode(Box::new(error)))?;
    control_reply_fence(
        txn,
        conversation,
        json!({(key): {"generation": generation}}),
    )
    .await
}

/// Capture the latest saved daily summary and the generation that made it available.
pub(crate) async fn capture_today(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Uuid,
) -> Result<(String, String), sqlx::Error> {
    let fence = projection_fence(txn, conversation, "telegram_today").await?;
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

pub(crate) async fn capture_status(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Uuid,
    configured_timezone: &str,
    now: OffsetDateTime,
) -> Result<(String, String), sqlx::Error> {
    let fence = projection_fence(txn, conversation, "telegram_status").await?;
    let timezone = owner_timezone(txn, configured_timezone)
        .await
        .map_err(protocol_error)?;
    let at = StoredInstant::try_from(now).map_err(protocol_error)?;
    let report = data_freshness_in(txn, at, &timezone)
        .await
        .map_err(protocol_error)?;
    let days: i64 = sqlx::query_scalar("SELECT count(*) FROM health_days")
        .fetch_one(&mut **txn)
        .await?;
    let endpoints = report["endpoints"]
        .as_object()
        .ok_or_else(|| protocol_error("Invalid endpoint report"))?;
    let mut text = format!(
        "Связь с базой работает. Сохранено дней: {days}. Обновляемых источников: {}.\n{}",
        endpoints.len(),
        connection_status(&report["connection"]),
    );
    if let Some(success) = endpoints
        .values()
        .filter_map(|value| value["success_at"].as_str())
        .filter(|success| !success.is_empty())
        .max()
    {
        let instant = success.parse().map_err(protocol_error)?;
        let (date, clock) = source_calendar_wall_time(instant, &timezone)
            .ok_or_else(|| protocol_error("Invalid owner timezone"))?;
        let day = date.day();
        let month = u8::from(date.month());
        text.push_str(&format!(
            " Последний успешный ответ Garmin: {day:02}.{month:02} {:02}:{:02}.",
            clock.hour(),
            clock.minute(),
        ));
    }
    if report["archive_replay"]["ready"] != true {
        text.push_str("\nПересчёт архива не завершён; анализ Garmin временно недоступен.");
        return Ok((text, fence));
    }

    let hr = &report["channels"]["heart_rate_bpm"];
    text.push_str("\nПульс часов: ");
    if let Some(lag) = hr["observation_lag_seconds"].as_f64() {
        text.push_str(&format!("последнее измерение {:.1} ч назад.", lag / 3600.0));
    } else {
        text.push_str("нет сохранённых измерений.");
    }
    if hr["usable_for_current_state"] != true {
        text.push_str(" Данных недостаточно для оценки текущего состояния.");
    }
    if let Some(coverage) = hr["coverage_ratio"].as_f64() {
        text.push_str(&format!(
            " Покрытие дня без заполнения пропусков: {:.0}%.",
            coverage * 100.0
        ));
    }
    text.push_str("\nНочной HRV: ");
    if let Some(date) = report["channels"]["hrv_nightly_avg"]["source_calendar_date"]
        .as_str()
        .filter(|date| !date.is_empty())
    {
        text.push_str(&format!("сводка за {date}."));
    } else {
        text.push_str("нет данных.");
    }
    Ok((text, fence))
}

fn connection_status(connection: &Value) -> String {
    match connection["status"].as_str() {
        Some("reauth_required") if connection["reason_class"].as_str().is_some_and(|reason| reason.starts_with("Account")) =>
            "Garmin: синхронизация остановлена, проверьте привязку владельца аккаунта локально.".into(),
        Some("reauth_required") => "Garmin: нужен повторный локальный вход (garmin-ai login) и перезапуск worker. История и дневник доступны.".into(),
        Some(status @ ("rate_limited" | "degraded")) => {
            let label = if status == "rate_limited" { "лимит запросов" } else { "ошибка соединения" };
            let deadline = connection["blocked_until"].as_str().filter(|value| !value.is_empty()).unwrap_or("паузы");
            format!("Garmin: {label}; автоматическая повторная попытка после {deadline}.")
        }
        Some("active") => "Garmin: соединение восстановлено.".into(),
        _ => "Garmin: состояние соединения ещё не определено.".into(),
    }
}

fn protocol_error(error: impl std::fmt::Display) -> sqlx::Error {
    sqlx::Error::Protocol(error.to_string())
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
