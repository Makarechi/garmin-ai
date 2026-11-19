use std::{error::Error, fmt};

use ga_domain::{
    hypothesis::{HypothesisSpec, classify_validation},
    timestamp::{StoredInstant, python_isoformat_microseconds},
};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{HealthAnalysisError, begin_daily_analysis, coffee::coffee_sleep_in, owner_timezone};
use crate::{
    Database,
    diary::DiaryWriteError,
    replay::{GARMIN_PARSER_VERSION, projection_pending},
};

#[derive(Debug)]
pub enum HypothesisError {
    Missing,
    Conflict(&'static str),
    Value(&'static str),
    Spec,
    Calendar,
    Health(HealthAnalysisError),
    Write(DiaryWriteError),
    Database,
}

impl fmt::Display for HypothesisError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Missing => "Hypothesis not found",
            Self::Conflict(message) | Self::Value(message) => message,
            Self::Spec => "Hypothesis specification is invalid",
            Self::Calendar => "Hypothesis calendar conversion failed",
            Self::Health(_) | Self::Write(_) | Self::Database => {
                "Hypothesis storage is unavailable"
            }
        })
    }
}
impl Error for HypothesisError {}
impl From<sqlx::Error> for HypothesisError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<HealthAnalysisError> for HypothesisError {
    fn from(error: HealthAnalysisError) -> Self {
        Self::Health(error)
    }
}
impl From<DiaryWriteError> for HypothesisError {
    fn from(error: DiaryWriteError) -> Self {
        Self::Write(error)
    }
}

async fn fetch(
    txn: &mut Transaction<'_, Postgres>,
    identity: Uuid,
) -> Result<Value, HypothesisError> {
    let encoded: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(format!("hypothesis:{identity}"))
            .fetch_optional(&mut **txn)
            .await?;
    serde_json::from_str(&encoded.ok_or(HypothesisError::Missing)?)
        .map_err(|_| HypothesisError::Database)
}

async fn generation(txn: &mut Transaction<'_, Postgres>) -> Result<Value, HypothesisError> {
    let encoded: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='replay:generation'")
            .fetch_optional(&mut **txn)
            .await?;
    match encoded {
        Some(encoded) => serde_json::from_str(&encoded).map_err(|_| HypothesisError::Database),
        None => Ok(Value::Null),
    }
}

fn registered_method(value: &Value) -> &Value {
    let fallback = &value["discovery"]["spec"]["method_version"];
    match &value["method_version"] {
        Value::Null | Value::Bool(false) => fallback,
        Value::String(value) if value.is_empty() => fallback,
        Value::Number(value) if value.as_f64() == Some(0.0) => fallback,
        Value::Array(value) if value.is_empty() => fallback,
        Value::Object(value) if value.is_empty() => fallback,
        method => method,
    }
}

async fn save(
    txn: &mut Transaction<'_, Postgres>,
    identity: Uuid,
    value: &Value,
) -> Result<(), HypothesisError> {
    let encoded = serde_json::to_string(value).map_err(|_| HypothesisError::Database)?;
    let saved = sqlx::query("UPDATE app_state SET value=$2::jsonb WHERE key=$1")
        .bind(format!("hypothesis:{identity}"))
        .bind(encoded)
        .execute(&mut **txn)
        .await?;
    if saved.rows_affected() != 1 {
        return Err(HypothesisError::Database);
    }
    Ok(())
}

async fn today(
    txn: &mut Transaction<'_, Postgres>,
    spec: &HypothesisSpec,
    now: StoredInstant,
    configured: &str,
) -> Result<time::Date, HypothesisError> {
    let timezone = owner_timezone(txn, configured).await?;
    if spec.document()["timezone"].as_str() != Some(&timezone) {
        return Err(HypothesisError::Value(
            "Hypothesis timezone must match the configured data timezone",
        ));
    }
    spec.local_date(now).ok_or(HypothesisError::Calendar)
}

async fn analyze(
    txn: &mut Transaction<'_, Postgres>,
    spec: &HypothesisSpec,
    validation: bool,
) -> Result<Value, HypothesisError> {
    if projection_pending(txn, GARMIN_PARSER_VERSION).await? {
        return Err(HypothesisError::Health(HealthAnalysisError::ReplayPending));
    }
    let (start, end) = if validation {
        spec.validation_period()
    } else {
        spec.discovery_period()
    };
    let hours = spec.document()["late_hours"]
        .as_f64()
        .ok_or(HypothesisError::Spec)?;
    let outcome = spec.document()["outcome"]
        .as_str()
        .ok_or(HypothesisError::Spec)?;
    coffee_sleep_in(txn, start, end, Some(hours), outcome)
        .await
        .map_err(HypothesisError::Health)
}

impl Database {
    pub async fn hypothesis(&self, identity: Uuid) -> Result<Value, HypothesisError> {
        let mut txn = begin_daily_analysis(&self.work).await?;
        let value = fetch(&mut txn, identity).await?;
        txn.commit().await?;
        Ok(value)
    }

    pub async fn register_hypothesis(
        &self,
        owner: Uuid,
        spec: &HypothesisSpec,
        now: StoredInstant,
        configured: &str,
    ) -> Result<Value, HypothesisError> {
        let spec = HypothesisSpec::from_json(spec.document()).map_err(|_| HypothesisError::Spec)?;
        let mut txn = self.begin_diary_write(owner).await?;
        match fetch(&mut txn, spec.id()).await {
            Ok(existing) => {
                if !spec.matches(&existing["spec"]) {
                    return Err(HypothesisError::Conflict(
                        "Registered hypothesis specification is immutable",
                    ));
                }
                txn.commit().await?;
                return Ok(existing);
            }
            Err(HypothesisError::Missing) => (),
            Err(error) => return Err(error),
        }
        let current = today(&mut txn, &spec, now, configured).await?;
        let (_, discovery_end) = spec.discovery_period();
        let (validation_start, _) = spec.validation_period();
        if !(discovery_end < current && current < validation_start) {
            return Err(HypothesisError::Value(
                "Register after discovery and before prospective validation starts",
            ));
        }
        if !(3..=31).contains(&(validation_start - current).whole_days()) {
            return Err(HypothesisError::Value(
                "Prospective exposure needs a buffer: start 3 to 31 days after registration",
            ));
        }
        let count: i64 =
            sqlx::query_scalar("SELECT count(*) FROM app_state WHERE key LIKE 'hypothesis:%'")
                .fetch_one(&mut *txn)
                .await?;
        if count >= 100 {
            return Err(HypothesisError::Value("Hypothesis storage limit reached"));
        }
        let discovery = analyze(&mut txn, &spec, false).await?;
        let generation = generation(&mut txn).await?;
        let mut value = json!({
            "spec":spec.document(),"created_at":now.to_string(),
            "method_version":discovery["spec"]["method_version"],
            "normalization_parser_version":GARMIN_PARSER_VERSION,"projection_generation":generation,
            "status":"registered","discovery":null,"checks":[],"check_count":0,"current_check_index":null,
            "interpretation":"Observational protocol, not a treatment experiment or verified recommendation",
        });
        value["discovery"] = discovery;
        sqlx::query("INSERT INTO app_state(key,value) VALUES ($1,$2::jsonb)")
            .bind(format!("hypothesis:{}", spec.id()))
            .bind(serde_json::to_string(&value).map_err(|_| HypothesisError::Database)?)
            .execute(&mut *txn)
            .await?;
        txn.commit().await?;
        Ok(value)
    }

    pub async fn recheck_hypothesis(
        &self,
        owner: Uuid,
        identity: Uuid,
        now: StoredInstant,
        configured: &str,
    ) -> Result<Value, HypothesisError> {
        let mut txn = self.begin_diary_write(owner).await?;
        let mut value = fetch(&mut txn, identity).await?;
        if value["normalization_parser_version"].as_f64() != Some(f64::from(GARMIN_PARSER_VERSION))
        {
            return Err(HypothesisError::Conflict(
                "Normalization parser changed or is unknown; register a new hypothesis",
            ));
        }
        if value.get("projection_generation").is_none()
            || value["projection_generation"] != generation(&mut txn).await?
        {
            return Err(HypothesisError::Conflict(
                "Normalization projection changed or is unknown; register a new hypothesis",
            ));
        }
        let spec = HypothesisSpec::from_json(&value["spec"]).map_err(|_| HypothesisError::Spec)?;
        let current = today(&mut txn, &spec, now, configured).await?;
        if value["status"] == "stopped" || current >= spec.expires() {
            return Err(HypothesisError::Conflict("Hypothesis stopped or expired"));
        }
        if current <= spec.validation_period().1 {
            return Err(HypothesisError::Value("Validation period has not finished"));
        }
        if registered_method(&value).as_str() != Some("coffee-sleep-v1") {
            return Err(HypothesisError::Conflict(
                "Registered analyzer version is unavailable; register a new protocol",
            ));
        }
        let result = analyze(&mut txn, &spec, true).await?;
        if result["spec"]["method_version"] != "coffee-sleep-v1" {
            return Err(HypothesisError::Conflict(
                "Validation analyzer version differs from the registered protocol",
            ));
        }
        let created = value["created_at"]
            .as_str()
            .and_then(python_isoformat_microseconds)
            .ok_or(HypothesisError::Spec)?;
        for row in result["rows"].as_array().ok_or(HypothesisError::Database)? {
            if let Some(start) = row["exposure_start"]
                .as_str()
                .filter(|start| !start.is_empty())
            {
                let start = python_isoformat_microseconds(start).ok_or(HypothesisError::Spec)?;
                if start < created {
                    return Err(HypothesisError::Conflict(
                        "Validation exposure predates prospective registration",
                    ));
                }
            }
        }
        let checks = value["checks"]
            .as_array()
            .ok_or(HypothesisError::Database)?;
        if let Some(index) = checks
            .iter()
            .position(|check| check["evidence"]["evidence_hash"] == result["evidence_hash"])
        {
            value["current_check_index"] = json!(index);
            value["last_checked_at"] = json!(now.to_string());
        } else {
            if checks.len() >= 10 {
                return Err(HypothesisError::Value(
                    "Recheck history limit reached; retained results are not overwritten",
                ));
            }
            let index = checks.len();
            let direction = spec.document()["direction"]
                .as_str()
                .ok_or(HypothesisError::Spec)?;
            let conclusion = classify_validation(direction, &value["discovery"], &result);
            value["checks"]
                .as_array_mut()
                .ok_or(HypothesisError::Database)?
                .push(
                    json!({"checked_at":now.to_string(),"conclusion":conclusion,"evidence":result}),
                );
            value["status"] = json!("checked");
            value["check_count"] = json!(index + 1);
            value["current_check_index"] = json!(index);
            value["last_checked_at"] = json!(now.to_string());
        }
        save(&mut txn, identity, &value).await?;
        txn.commit().await?;
        Ok(value)
    }

    pub async fn stop_hypothesis(
        &self,
        owner: Uuid,
        identity: Uuid,
        now: StoredInstant,
    ) -> Result<Value, HypothesisError> {
        let mut txn = self.begin_diary_write(owner).await?;
        let mut value = fetch(&mut txn, identity).await?;
        if value["status"] != "stopped" {
            value["status"] = json!("stopped");
            value["stopped_at"] = json!(now.to_string());
            save(&mut txn, identity, &value).await?;
        }
        txn.commit().await?;
        Ok(value)
    }
}
