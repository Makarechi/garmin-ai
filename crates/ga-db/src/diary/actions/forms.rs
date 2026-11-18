use std::time::Duration;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ActionRecord, TrackerActionError, localized_label, read_locale};
use crate::{Database, diary::DiaryReadError};

mod file;
pub(super) mod projection;
mod submit;

pub use file::{FileImportOutcome, FileImportStoreError};
pub use submit::{FormSubmission, FormSubmitError};

enum FormAction {
    Create(Uuid),
    Edit(Uuid, i64),
}

impl FormAction {
    fn parse(raw: &str) -> Result<Self, TrackerActionError> {
        let mut parts = raw.split(':');
        let kind = parts.next().ok_or(TrackerActionError::Missing)?;
        let identity = parts
            .next()
            .and_then(|id| id.parse().ok())
            .ok_or(TrackerActionError::Missing)?;
        match (kind, parts.next(), parts.next()) {
            ("create", None, None) => Ok(Self::Create(identity)),
            ("edit", Some(revision), None) => Ok(Self::Edit(
                identity,
                revision
                    .trim()
                    .parse()
                    .map_err(|_| TrackerActionError::Missing)?,
            )),
            _ => Err(TrackerActionError::Missing),
        }
    }
}

struct FormContract {
    action: ActionRecord,
    topology: String,
    hash: String,
    schema: String,
    metadata: String,
}

impl FromRow<'_, PgRow> for FormContract {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            action: ActionRecord::from_row(row)?,
            topology: row.try_get("topology")?,
            hash: row.try_get("schema_hash")?,
            schema: row.try_get("schema")?,
            metadata: row.try_get("field_metadata")?,
        })
    }
}

struct InitialEntry {
    id: Uuid,
    revision: i32,
    version: Uuid,
    payload: String,
    start: OffsetDateTime,
    end: Option<OffsetDateTime>,
    topology: String,
    timezone: String,
    evidence: String,
}

impl FromRow<'_, PgRow> for InitialEntry {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            id: row.try_get("id")?,
            revision: row.try_get("revision")?,
            version: row
                .try_get::<Option<_>, _>("definition_version_id")?
                .ok_or_else(|| sqlx::Error::Protocol("Missing definition version".into()))?,
            payload: row.try_get("payload")?,
            start: row.try_get("start")?,
            end: row.try_get("end")?,
            topology: row.try_get("topology")?,
            timezone: row.try_get("timezone")?,
            evidence: row.try_get("evidence")?,
        })
    }
}

const VERSION: &str = "SELECT d.key,v.id AS version_id,v.labels::text,t.shortcut,
                              v.topology,v.schema_hash,v.schema::text,v.field_metadata::text
                       FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id
                       LEFT JOIN tracker_configs t ON t.definition_id=d.id
                       WHERE v.id=$1 AND d.owner_id=$2 AND d.namespace='user' AND (
                         ($3 AND d.status='active' AND d.current_version=v.version
                              AND v.allowed_operations ? 'create')
                         OR (NOT $3 AND v.allowed_operations ?& ARRAY['update','query'])
                       )";

impl Database {
    pub async fn tracker_generated_form(
        &self,
        owner: Uuid,
        action_id: &str,
        locale: Option<&str>,
        configured_locale: &str,
    ) -> Result<Value, TrackerActionError> {
        let action = FormAction::parse(action_id)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let locale = read_locale(&mut txn, owner, locale, configured_locale).await?;
            let (contract, entry) = form_contract(&mut txn, owner, action).await?;
            let form = contract.project(entry, &locale)?;
            txn.commit().await?;
            Ok(form)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }
}

async fn form_contract(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    action: FormAction,
) -> Result<(FormContract, Option<InitialEntry>), TrackerActionError> {
    match action {
        FormAction::Create(version) => {
            let row = sqlx::query_as(VERSION)
                .bind(version)
                .bind(owner)
                .bind(true)
                .fetch_optional(&mut **txn)
                .await?
                .ok_or(TrackerActionError::InactiveContract)?;
            Ok((row, None))
        }
        FormAction::Edit(id, revision) => {
            let entry: InitialEntry = sqlx::query_as(
                "SELECT id,revision,definition_version_id,payload::text,start,\"end\",topology,timezone,
                    coalesce(evidence_refs,'[]'::jsonb)::text AS evidence
                 FROM events WHERE id=$1 AND NOT deleted AND definition_version_id IS NOT NULL",
            ).bind(id).fetch_optional(&mut **txn).await?.ok_or(TrackerActionError::Missing)?;
            let row = sqlx::query_as(VERSION)
                .bind(entry.version)
                .bind(owner)
                .bind(false)
                .fetch_optional(&mut **txn)
                .await?
                .ok_or(TrackerActionError::Missing)?;
            if i64::from(entry.revision) != revision {
                return Err(TrackerActionError::EntryChanged);
            }
            Ok((row, Some(entry)))
        }
    }
}

impl FormContract {
    fn project(
        &self,
        entry: Option<InitialEntry>,
        locale: &str,
    ) -> Result<Value, TrackerActionError> {
        let schema: Value = parse(&self.schema)?;
        let metadata: Value = parse(&self.metadata)?;
        let labels: Value = parse(&self.action.labels)?;
        let action = self.action.action(
            locale,
            entry.as_ref().map(|entry| (entry.id, entry.revision)),
        )?;
        let title = localized_label(&labels, locale)?;
        let (complex, conditional) = projection::flags(&schema)?;
        let mut initial = initial_values(entry.as_ref(), &metadata)?;
        initial.extend([
            ("id".into(), action["id"].clone()),
            ("action".into(), action),
            ("title".into(), json!(title)),
            ("topology".into(), json!(self.topology)),
            ("schema_hash".into(), json!(self.hash)),
            ("complex_schema".into(), json!(complex)),
            (
                "submission_id".into(),
                json!(entry.is_none().then(|| Uuid::new_v4().simple().to_string())),
            ),
            (
                "fields".into(),
                json!(projection::fields(&schema, &metadata, locale)?),
            ),
            ("conditional_requirements".into(), json!(conditional)),
        ]);
        Ok(Value::Object(initial))
    }
}

fn initial_values(
    entry: Option<&InitialEntry>,
    metadata: &Value,
) -> Result<Map<String, Value>, TrackerActionError> {
    let default = || Value::Object(Map::new());
    let mut values = entry
        .map(|entry| parse(&entry.payload))
        .transpose()?
        .unwrap_or_else(default);
    let values = values
        .as_object_mut()
        .ok_or(TrackerActionError::InvalidContract)?;
    values.remove("type");
    let units: Map<_, _> = values
        .keys()
        .filter_map(|name| {
            let unit = metadata[name]["unit"]
                .as_str()
                .filter(|unit| !unit.is_empty())?;
            Some((name.clone(), json!(unit)))
        })
        .collect();
    let (start, end, topology, timezone, evidence) = if let Some(entry) = entry {
        let start = timestamp(entry.start)?;
        let end = entry.end.map(timestamp).transpose()?;
        let evidence = parse(&entry.evidence)?;
        (
            Some(start),
            end,
            Some(&entry.topology),
            Some(&entry.timezone),
            evidence,
        )
    } else {
        (None, None, None, None, json!([]))
    };
    Ok(Map::from_iter([
        ("initial_values".into(), json!(values)),
        ("initial_units".into(), Value::Object(units)),
        ("initial_start".into(), json!(start)),
        ("initial_end".into(), json!(end)),
        ("initial_topology".into(), json!(topology)),
        ("initial_timezone".into(), json!(timezone)),
        ("initial_evidence_refs".into(), evidence),
    ]))
}

fn parse(raw: &str) -> Result<Value, TrackerActionError> {
    serde_json::from_str(raw).map_err(|_| TrackerActionError::InvalidContract)
}

fn timestamp(at: OffsetDateTime) -> Result<String, TrackerActionError> {
    let value = StoredInstant::try_from(at)
        .map_err(|_| TrackerActionError::InvalidContract)?
        .to_string();
    Ok(value
        .strip_suffix("+00:00")
        .map_or_else(|| value.clone(), |utc| format!("{utc}Z")))
}
