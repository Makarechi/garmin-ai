use ga_domain::{timestamp::StoredInstant, tracker_nl::TrackerTextRequest};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool, Row, postgres::PgRow};
use std::{collections::BTreeMap, error::Error};
use time::OffsetDateTime;
use uuid::Uuid;

pub(crate) fn normalize(
    value: Value,
    identities: &BTreeMap<String, String>,
) -> Result<Value, Box<dyn Error>> {
    Ok(match value {
        Value::Object(fields) => {
            let mut normalized = serde_json::Map::new();
            for (key, mut item) in fields {
                if matches!(key.as_str(), "created_at" | "prompt_advanced_at") {
                    assert!(StoredInstant::from_legacy_input(&item).is_some());
                    continue;
                }
                if key == "submission_id" && !item.is_null() {
                    let nonce = item.as_str().ok_or("Expected submission nonce")?;
                    assert_eq!(nonce.len(), 32);
                    assert!(nonce.bytes().all(|c| c.is_ascii_hexdigit()));
                    item = json!("synthetic-submission");
                }
                normalized.insert(key, normalize(item, identities)?);
            }
            Value::Object(normalized)
        }
        Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(|item| normalize(item, identities))
                .collect::<Result<Vec<_>, _>>()?,
        ),
        Value::String(mut text) => {
            for (id, key) in identities {
                text = text.replace(id, key);
            }
            json!(text)
        }
        other => other,
    })
}

pub(crate) async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let versions:Vec<(Uuid,String)>=sqlx::query_as("SELECT v.id,d.key FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id").fetch_all(pool).await?;
    let mut identities = versions
        .into_iter()
        .map(|(id, key)| (id.to_string(), key))
        .collect::<BTreeMap<_, _>>();
    struct Event {
        id: Uuid,
        kind: String,
        start: OffsetDateTime,
        end: Option<OffsetDateTime>,
        timezone: String,
        topology: String,
        revision: i32,
        deleted: bool,
        source: String,
        payload: String,
    }
    impl<'row> FromRow<'row, PgRow> for Event {
        fn from_row(row: &'row PgRow) -> Result<Self, sqlx::Error> {
            Ok(Self {
                id: row.try_get("id")?,
                kind: row.try_get("kind")?,
                start: row.try_get("start")?,
                end: row.try_get("end")?,
                timezone: row.try_get("timezone")?,
                topology: row.try_get("topology")?,
                revision: row.try_get("revision")?,
                deleted: row.try_get("deleted")?,
                source: row.try_get("source")?,
                payload: row.try_get("payload")?,
            })
        }
    }
    let rows:Vec<Event>=sqlx::query_as("SELECT id,kind,start,\"end\",timezone,topology,revision,deleted,source,payload::text FROM events WHERE starts_with(kind,'user.synthetic_chat_') ORDER BY start,kind,id").fetch_all(pool).await?;
    let mut events = Vec::new();
    for row in rows {
        let start = StoredInstant::try_from(row.start)?.to_string();
        identities.insert(row.id.to_string(), format!("entry:{}:{start}", row.kind));
        events.push(json!({"kind":row.kind,"start":start,"end":row.end.map(StoredInstant::try_from).transpose()?.map(|at|at.to_string()),"timezone":row.timezone,"topology":row.topology,"revision":row.revision,"deleted":row.deleted,"source":row.source,"payload":serde_json::from_str::<Value>(&row.payload)?}));
    }
    let pending: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='conversation:pending'")
            .fetch_optional(pool)
            .await?;
    let audit:Vec<(String,OffsetDateTime,String,String,i64)>=sqlx::query_as("SELECT e.kind,e.start,a.action,a.actor,count(*) FROM audit_log a JOIN events e ON e.id=a.event_id WHERE starts_with(e.kind,'user.synthetic_chat_') GROUP BY e.kind,e.start,a.action,a.actor ORDER BY e.kind,e.start,a.action,a.actor").fetch_all(pool).await?;
    let audit = audit
        .into_iter()
        .map(|(kind, start, action, actor, count)| {
            Ok(json!([
                kind,
                StoredInstant::try_from(start)?.to_string(),
                action,
                actor,
                count
            ]))
        })
        .collect::<Result<Vec<Value>, Box<dyn Error>>>()?;
    Ok(
        json!({"pending":normalize(pending.map(|raw|serde_json::from_str(&raw)).transpose()?.unwrap_or(Value::Null),&identities)?,"events":events,"audit":audit}),
    )
}

pub(crate) async fn with_nl_state(pool: &PgPool, cases: &[Value]) -> Result<Value, Box<dyn Error>> {
    let mut snapshot = snapshot(pool).await?;
    let versions: Vec<(Uuid, String)> = sqlx::query_as(
        "SELECT v.id,d.key FROM event_definition_versions v
        JOIN event_definitions d ON d.id=v.definition_id",
    )
    .fetch_all(pool)
    .await?;
    let mut identities = versions
        .iter()
        .map(|(id, key)| (id.to_string(), key.clone()))
        .collect::<BTreeMap<_, _>>();
    let events: Vec<(Uuid, String, OffsetDateTime, Option<String>, String)> = sqlx::query_as(
        "SELECT id,kind,start,original_text,evidence_refs::text FROM events
        WHERE starts_with(kind,'user.synthetic_chat_') ORDER BY start,kind,id",
    )
    .fetch_all(pool)
    .await?;
    let mut provenance = Vec::new();
    for (id, kind, start, original_text, refs) in events {
        identities.insert(
            id.to_string(),
            format!("entry:{kind}:{}", StoredInstant::try_from(start)?),
        );
        provenance.push(
            json!({"kind":kind,"start":StoredInstant::try_from(start)?.to_string(),
            "original_text":original_text,"evidence_refs":serde_json::from_str::<Value>(&refs)?}),
        );
    }
    let mut states = serde_json::Map::new();
    for (index, case) in cases.iter().enumerate() {
        let Some(name) = case["legacy_tracker"].as_str() else {
            continue;
        };
        let text = case["message"]["text"]
            .as_str()
            .or_else(|| case["message"]["caption"].as_str())
            .unwrap_or("");
        if text.is_empty() {
            continue;
        }
        let key = format!("user.synthetic_chat_{name}");
        let version = versions
            .iter()
            .find(|(_, name)| name == &key)
            .ok_or("Expected fixture version")?
            .0;
        let operation_id = format!("telegram:{}", 71001 + index);
        let request = TrackerTextRequest::from_value(&json!({"text":text,
            "operation_id":operation_id,"selected_definition_version_id":version.to_string()}))?;
        let operation_key = request.operation_key("telegram:42");
        for prefix in ["nl-context", "nl-operation"] {
            let state: Option<String> =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                    .bind(operation_key.replacen("nl-operation", prefix, 1))
                    .fetch_optional(pool)
                    .await?;
            if let Some(state) = state {
                let mut value: Value = serde_json::from_str(&state)?;
                assert!(
                    request
                        .request_hashes()?
                        .iter()
                        .any(|hash| value["request_hash"].as_str() == Some(hash.as_str()))
                );
                value
                    .as_object_mut()
                    .ok_or("Expected stored tracker text state")?
                    .remove("request_hash");
                states.insert(
                    format!("{prefix}:{operation_id}"),
                    normalize(value, &identities)?,
                );
            }
        }
    }
    snapshot["nl_state"] = Value::Object(states);
    snapshot["provenance"] = normalize(Value::Array(provenance), &identities)?;
    Ok(snapshot)
}
