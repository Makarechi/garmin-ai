mod contract;
pub use contract::{CalendarBatch, CalendarSources};

use std::time::Duration;

use futures_util::TryStreamExt;
use ga_domain::{legacy_json::integer_argument, timestamp::StoredInstant};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use self::contract::{CalendarItem, checked_range};
use crate::{Database, check_readiness};

const PREFIX: &str = "calendar-context:";
const STORAGE_LIMIT: i64 = 2000;

#[derive(Debug)]
pub enum CalendarError {
    Invalid,
    Conflict,
    Database,
}

impl From<sqlx::Error> for CalendarError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

#[derive(Deserialize, Serialize)]
struct Revision {
    revision: Value,
    hash: String,
}

#[derive(Deserialize, Serialize)]
struct StoredPlan {
    item: Value,
    hash: String,
    received_at: String,
    previous_revisions: Vec<Revision>,
}

impl Database {
    pub async fn import_calendar_plans(
        &self,
        owner: Uuid,
        sources: &CalendarSources,
        batch: &CalendarBatch,
    ) -> Result<Value, CalendarError> {
        tokio::time::timeout(Duration::from_secs(4), async {
            let mut txn = self
                .begin_diary_write(owner)
                .await
                .map_err(|_| CalendarError::Database)?;
            let now = clock(&mut txn).await?;
            let allowed = sources.allowed(now);
            let mut count: i64 =
                sqlx::query_scalar("SELECT count(*) FROM app_state WHERE starts_with(key,$1)")
                    .bind(PREFIX)
                    .fetch_one(&mut *txn)
                    .await?;
            let mut outcomes = Vec::with_capacity(batch.0.len());
            for item in &batch.0 {
                if !item.permitted(&allowed) {
                    return Err(CalendarError::Invalid);
                }
                let status = save(&mut txn, item, now, &mut count).await?;
                outcomes.push(json!({"id":item.id.to_string(),"status":status}));
            }
            txn.commit().await?;
            Ok(json!({"outcomes":outcomes}))
        })
        .await
        .map_err(|_| CalendarError::Database)?
    }

    pub async fn calendar_plans(
        &self,
        owner: Uuid,
        sources: &CalendarSources,
        start: StoredInstant,
        end: StoredInstant,
    ) -> Result<Value, CalendarError> {
        checked_range(start, end)?;
        tokio::time::timeout(Duration::from_secs(3), async {
            let mut txn = self.control.begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY").await?;
            sqlx::query("SET LOCAL statement_timeout='750ms'").execute(&mut *txn).await?;
            check_readiness(&mut txn).await.map_err(|_| CalendarError::Database)?;
            let enrolled: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM people WHERE singleton AND id=$1)")
                .bind(owner).fetch_one(&mut *txn).await?;
            if !enrolled {
                return Err(CalendarError::Database);
            }
            let allowed = sources.allowed(clock(&mut txn).await?);
            let mut rows = Vec::new();
            if !allowed.is_empty() {
                let mut values = sqlx::query_scalar::<_, String>(
                    "SELECT jsonb_set(value,'{previous_revisions}','[]'::jsonb)::text
                     FROM app_state WHERE starts_with(key,$1) ORDER BY key LIMIT $2",
                )
                .bind(PREFIX).bind(STORAGE_LIMIT+1).fetch(&mut *txn);
                while let Some(value) = values.try_next().await? {
                    let recorded = parse_stored(&value)?;
                    let source: Uuid = recorded.item["source_id"].as_str()
                        .and_then(|id| id.parse().ok()).ok_or(CalendarError::Database)?;
                    if !allowed.contains_key(&source) || recorded.item["status"] != "busy" {
                        continue;
                    }
                    let item = CalendarItem::parse(recorded.item).map_err(|_| CalendarError::Database)?;
                    if item.permitted(&allowed) && let Some((from, to, _)) = item.interval
                        && from < end && to > start
                    {
                        let mut view = item.document;
                        view["evidence_hash"] = json!(recorded.hash);
                        view["received_at"] = json!(recorded.received_at);
                        rows.push(view);
                    }
                }
            }
            txn.commit().await?;
            Ok(json!({
                "availability":if allowed.is_empty() {"disabled"} else {"enabled"},
                "rows":rows, "evidence_type":"calendar_plan", "limitations":[
                    "A busy interval is a plan, not evidence of attendance, physical activity or stress",
                    "Missing plans do not establish free time",
                    "Only explicitly enabled calendars and categories are included",
                    "No calendar text is sent to a language model",
                ],
            }))
        })
        .await
        .map_err(|_| CalendarError::Database)?
    }
}

async fn save<'a>(
    txn: &mut Transaction<'_, Postgres>,
    item: &'a CalendarItem,
    now: StoredInstant,
    count: &mut i64,
) -> Result<&'a str, CalendarError> {
    let key = format!("{PREFIX}{}:{}", item.source, item.id);
    let existing: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1 FOR UPDATE")
            .bind(&key)
            .fetch_optional(&mut **txn)
            .await?;
    let previous = if let Some(existing) = existing {
        let recorded = parse_stored(&existing)?;
        let revision =
            integer_argument(&item.document["revision"]).ok_or(CalendarError::Database)?;
        let current =
            integer_argument(&recorded.item["revision"]).ok_or(CalendarError::Database)?;
        if revision < current {
            if recorded.previous_revisions.iter().any(|previous| {
                previous.revision == item.document["revision"] && previous.hash != item.digest
            }) {
                return Err(CalendarError::Conflict);
            }
            return Ok("stale");
        }
        if revision == current {
            if recorded.hash != item.digest {
                return Err(CalendarError::Conflict);
            }
            return Ok("unchanged");
        }
        let mut history = recorded.previous_revisions;
        history.push(Revision {
            revision: recorded.item["revision"].clone(),
            hash: recorded.hash,
        });
        if history.len() > 20 {
            history.remove(0);
        }
        history
    } else {
        if *count >= STORAGE_LIMIT {
            return Err(CalendarError::Invalid);
        }
        *count += 1;
        Vec::new()
    };
    let value = StoredPlan {
        item: item.document.clone(),
        hash: item.digest.clone(),
        received_at: now.to_string(),
        previous_revisions: previous,
    };
    let encoded = serde_json::to_string(&value).map_err(|_| CalendarError::Database)?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()",
    )
    .bind(key)
    .bind(encoded)
    .execute(&mut **txn)
    .await?;
    item.document["status"]
        .as_str()
        .ok_or(CalendarError::Database)
}

fn parse_stored(text: &str) -> Result<StoredPlan, CalendarError> {
    let record: StoredPlan = serde_json::from_str(text).map_err(|_| CalendarError::Database)?;
    if record.previous_revisions.len() > 20
        || !valid_hash(&record.hash)
        || record
            .previous_revisions
            .iter()
            .any(|entry| !valid_hash(&entry.hash))
        || StoredInstant::from_python_isoformat(&record.received_at).is_none()
    {
        return Err(CalendarError::Database);
    }
    Ok(record)
}

fn valid_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

async fn clock(txn: &mut Transaction<'_, Postgres>) -> Result<StoredInstant, CalendarError> {
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    StoredInstant::try_from(now).map_err(|_| CalendarError::Database)
}
