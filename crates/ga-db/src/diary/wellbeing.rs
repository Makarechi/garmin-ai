use std::time::Duration;

use futures_util::TryStreamExt;
use ga_domain::{
    identity::python_cursor_uuid,
    legacy_json::{Utf8Layout, encode},
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{FromRow, Postgres, QueryBuilder, Row, Transaction, postgres::PgRow};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    DiaryReadError, EventSnapshotRow, event_snapshot,
    reads::{check_range, event_document, model_diary_allowed},
};
use crate::{Database, scenario_packs::model_disallowed_event_kinds};

const FIELDS: [&str; 4] = ["energy", "restedness", "pain", "functional_impact"];
const FILTER: &str = "kind='wellbeing_observation' AND NOT deleted AND status='confirmed'
    AND source NOT IN ('inferred','wearable') AND start >= $1 AND start < $2";

struct Summary {
    reports: i64,
    ratings: Value,
}

impl FromRow<'_, PgRow> for Summary {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        let mut ratings = serde_json::Map::new();
        for field in FIELDS {
            ratings.insert(
                field.to_owned(),
                json!({
                    "count":row.try_get::<i64,_>(format!("{field}_count").as_str())?,
                    "mean":row.try_get::<Option<f64>,_>(format!("{field}_mean").as_str())?,
                    "min":row.try_get::<Option<f64>,_>(format!("{field}_min").as_str())?,
                    "max":row.try_get::<Option<f64>,_>(format!("{field}_max").as_str())?,
                }),
            );
        }
        Ok(Self {
            reports: row.try_get("reports")?,
            ratings: Value::Object(ratings),
        })
    }
}

impl Database {
    /// Page explicit subjective reports while aggregating ratings over the whole range.
    pub async fn wellbeing_observations(
        &self,
        start: StoredInstant,
        end: StoredInstant,
        cursor: Option<&str>,
        model_owner: Option<Uuid>,
    ) -> Result<Value, DiaryReadError> {
        let work = async {
            let mut txn = self.begin_diary_read().await?;
            if let Some(owner) = model_owner {
                require_model_wellbeing_in(&mut txn, owner).await?;
            }
            let result = wellbeing_observations_in(&mut txn, start, end, cursor).await?;
            txn.commit().await?;
            Ok(result)
        };
        tokio::time::timeout(Duration::from_millis(1800), work)
            .await
            .map_err(|_| DiaryReadError::Database)?
    }
}

pub(crate) async fn require_model_wellbeing_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<(), DiaryReadError> {
    model_diary_allowed(txn, owner).await?;
    if model_disallowed_event_kinds(txn)
        .await
        .map_err(|_| DiaryReadError::Database)?
        .contains(&"wellbeing_observation")
    {
        return Err(DiaryReadError::ModelConsent);
    }
    Ok(())
}

pub(crate) async fn wellbeing_observations_in(
    txn: &mut Transaction<'_, Postgres>,
    start: StoredInstant,
    end: StoredInstant,
    cursor: Option<&str>,
) -> Result<Value, DiaryReadError> {
    let (first, last) = (OffsetDateTime::from(start), OffsetDateTime::from(end));
    check_range(first, last)?;
    if last - first > time::Duration::days(31) {
        return Err(DiaryReadError::TimestampRange);
    }
    let after = cursor
        .map(|cursor| report_cursor(cursor, first, last))
        .transpose()?;
    let mut summary_sql = QueryBuilder::<Postgres>::new("SELECT count(*) AS reports");
    for field in FIELDS {
        for (aggregate, suffix) in [
            ("count", "count"),
            ("avg", "mean"),
            ("min", "min"),
            ("max", "max"),
        ] {
            // Both identifiers come from the fixed report contract above.
            summary_sql.push(format!(
                ",{aggregate}((payload->>'{field}')::double precision) AS {field}_{suffix}"
            ));
        }
    }
    summary_sql.push(" FROM events WHERE ");
    summary_sql.push(FILTER);
    let summary: Summary = summary_sql
        .build_query_as()
        .bind(first)
        .bind(last)
        .fetch_one(&mut **txn)
        .await?;
    let mut result = json!({
        "start":start.to_string(),"end":end.to_string(),
        "scales":{
            "energy":{"min":0,"max":10,"higher_means":"more_reported_energy"},
            "restedness":{"min":0,"max":10,"higher_means":"more_reported_rest"},
            "pain":{"min":0,"max":10,"higher_means":"more_reported_pain"},
            "functional_impact":{"min":0,"max":10,"higher_means":"more_interference_with_daily_life"},
        },
        "summary":{"reports":summary.reports,"ratings":summary.ratings,
            "scope":"all_confirmed_reports_in_requested_range","notes_analyzed":false},
        "rows":[],"next_cursor":null,"truncated":false,
        "evidence_type":"subjective_diary","missingness":"unreported_is_unknown",
        "limitations":[
            "Reported wellbeing and Garmin scores are distinct outcomes; retain disagreement",
            "Numeric ratings are recorded only when explicitly reported; notes imply no numeric rating",
            "No reports do not establish absence of symptoms or good wellbeing",
        ],
    });
    let mut query = sqlx::query_as::<_, EventSnapshotRow>(include_str!("wellbeing.sql"))
        .bind(first)
        .bind(last)
        .bind(after.map(|(at, _)| at))
        .bind(after.map(|(_, id)| id))
        .fetch(&mut **txn);
    let mut accepted = 0;
    let mut next_cursor = None;
    while let Some(row) = query.try_next().await? {
        if accepted == 200 {
            result["truncated"] = json!(true);
            break;
        }
        let mut event =
            event_document(event_snapshot(row).map_err(|_| DiaryReadError::InvalidEvent)?)?;
        event["omitted_fields"] = json!(["original_text", "idempotency_key"]);
        result["rows"]
            .as_array_mut()
            .ok_or(DiaryReadError::InvalidEvent)?
            .push(event);
        if ordered_json(&result)?.len() > 19000 {
            result["rows"]
                .as_array_mut()
                .ok_or(DiaryReadError::InvalidEvent)?
                .pop();
            result["truncated"] = json!(true);
            break;
        }
        let event = &result["rows"][accepted];
        next_cursor = Some(
            String::from_utf8(ordered_json(&json!([event["start"], event["id"]]))?)
                .map_err(|_| DiaryReadError::InvalidEvent)?,
        );
        accepted += 1;
    }
    drop(query);
    if result["truncated"] == true {
        result["next_cursor"] = json!(next_cursor);
    }
    Ok(result)
}

fn ordered_json(value: &Value) -> Result<Vec<u8>, DiaryReadError> {
    let bytes = serde_json::to_vec(value).map_err(|_| DiaryReadError::InvalidEvent)?;
    encode(&bytes, Utf8Layout::Ordered, bytes.len()).map_err(|_| DiaryReadError::InvalidEvent)
}

fn report_cursor(
    value: &str,
    start: OffsetDateTime,
    end: OffsetDateTime,
) -> Result<(OffsetDateTime, Uuid), DiaryReadError> {
    let invalid = || DiaryReadError::TimestampRange;
    let parsed: Value = serde_json::from_str(value).map_err(|_| invalid())?;
    let parts = match parsed {
        Value::Array(parts) => parts,
        Value::Object(parts) => parts
            .into_iter()
            .map(|(key, _)| Value::String(key))
            .collect(),
        _ => return Err(invalid()),
    };
    if parts.len() != 2 {
        return Err(invalid());
    }
    let at = parts[0].as_str().ok_or_else(invalid)?;
    let at = StoredInstant::from_python_isoformat(at)
        .map(OffsetDateTime::from)
        .ok_or_else(invalid)?;
    if at < start || at >= end {
        return Err(invalid());
    }
    let id = parts[1]
        .as_str()
        .and_then(python_cursor_uuid)
        .ok_or_else(invalid)?;
    Ok((at, id))
}
