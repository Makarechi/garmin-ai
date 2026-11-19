use std::{collections::BTreeMap, fmt, time::Duration};

use ga_domain::{
    event_input::known_timezone,
    event_schema::{CustomEntry, tracker_form_issues},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{FormAction, TrackerActionError, form_contract, parse};
use crate::{
    Database,
    diary::{
        DiaryWriteError,
        custom_entries::{CreateCustomEntry, StoredCustomEntry, UpdateCustomEntry},
    },
};

#[derive(Debug)]
pub enum FormSubmitError {
    Action(TrackerActionError),
    Diary(DiaryWriteError),
    Invalid,
    Conflict(&'static str),
    Validation(Vec<Value>),
}

impl fmt::Display for FormSubmitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Action(error) => error.fmt(f),
            Self::Diary(error) => error.fmt(f),
            Self::Invalid => f.write_str("Invalid form submission"),
            Self::Conflict(detail) => f.write_str(detail),
            Self::Validation(_) => f.write_str("Form validation failed"),
        }
    }
}
impl std::error::Error for FormSubmitError {}
impl From<TrackerActionError> for FormSubmitError {
    fn from(value: TrackerActionError) -> Self {
        Self::Action(value)
    }
}
impl From<DiaryWriteError> for FormSubmitError {
    fn from(value: DiaryWriteError) -> Self {
        Self::Diary(value)
    }
}
impl From<sqlx::Error> for FormSubmitError {
    fn from(_: sqlx::Error) -> Self {
        Self::Diary(DiaryWriteError::Database)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FormSubmission {
    action_id: String,
    schema_hash: String,
    submission_id: Option<String>,
    operation_id: Option<String>,
    start: Value,
    end: Option<Value>,
    #[serde(default = "utc")]
    timezone: String,
    #[serde(default)]
    values: Map<String, Value>,
    #[serde(default)]
    units: BTreeMap<String, String>,
}

fn utc() -> String {
    "UTC".to_owned()
}

impl FormSubmission {
    pub fn parse(bytes: &[u8]) -> Result<Self, FormSubmitError> {
        let input: Self = serde_json::from_slice(bytes).map_err(|_| FormSubmitError::Invalid)?;
        if !hexadecimal(&input.schema_hash, 64)
            || input
                .submission_id
                .as_deref()
                .is_some_and(|value| !hexadecimal(value, 32))
            || input
                .operation_id
                .as_deref()
                .is_some_and(|value| !(1..=160).contains(&value.chars().count()))
            || input.values.len() > 32
            || input.units.len() > 32
            || !known_timezone(&input.timezone)
        {
            return Err(FormSubmitError::Invalid);
        }
        let (start, end) = input.window()?;
        if end.is_some_and(|end| end < start) {
            return Err(FormSubmitError::Invalid);
        }
        Ok(input)
    }

    fn window(&self) -> Result<(StoredInstant, Option<StoredInstant>), FormSubmitError> {
        let start =
            StoredInstant::from_legacy_input(&self.start).ok_or(FormSubmitError::Invalid)?;
        let end = self
            .end
            .as_ref()
            .map(|end| StoredInstant::from_legacy_input(end).ok_or(FormSubmitError::Invalid))
            .transpose()?;
        Ok((start, end))
    }

    fn issues(
        &self,
        schema: &Value,
        field_metadata: &Value,
        topology: &str,
    ) -> Result<Vec<Value>, FormSubmitError> {
        let mut issues = tracker_form_issues(schema, &Value::Object(self.values.clone()))
            .map_err(|_| TrackerActionError::InvalidContract)?;
        for (name, unit) in &self.units {
            if field_metadata[name]["unit"].as_str() != Some(unit) {
                issues.push(
                    json!({"field":name,"code":"unit","message":"Unit does not match the form"}),
                );
            }
        }
        let (start, end) = self.window()?;
        let time_issue = match topology {
            "point" if end.is_some_and(|end| end != start) => Some("End is not used"),
            "bounded_interval" if end.is_none_or(|end| end <= start) => {
                Some("A later end is required")
            }
            "open_interval" if end.is_some_and(|end| end < start) => {
                Some("End cannot precede start")
            }
            _ => None,
        };
        if let Some(message) = time_issue {
            issues.push(json!({"field":"end","code":"topology","message":message}));
        }
        Ok(issues)
    }

    fn idempotency(&self) -> Option<String> {
        self.submission_id
            .as_deref()
            .map(|nonce| format!("tracker-form:{nonce}"))
            .or_else(|| {
                self.operation_id
                    .as_deref()
                    .map(|operation| format!("form:{operation}"))
            })
    }
}

fn hexadecimal(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

impl Database {
    /// Validate a saved form version, then commit its entry and audit in one write transaction.
    pub async fn submit_tracker_form(
        &self,
        owner: Uuid,
        route: &str,
        form: &FormSubmission,
    ) -> Result<StoredCustomEntry, FormSubmitError> {
        if route != form.action_id {
            return Err(FormSubmitError::Conflict(
                "Form action does not match the route",
            ));
        }
        FormAction::parse(route)?;
        tokio::time::timeout(Duration::from_millis(2000), async {
            let mut txn = self.begin_diary_write(owner).await?;
            let result = Self::submit_tracker_form_in(
                &mut txn,
                route,
                form,
                TrackerFormWrite {
                    owner_id: owner,
                    actor: "api",
                    source: None,
                    idempotency_key: None,
                },
            )
            .await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| FormSubmitError::Diary(DiaryWriteError::Database))?
    }

    pub(crate) async fn submit_tracker_form_in(
        txn: &mut Transaction<'_, Postgres>,
        route: &str,
        form: &FormSubmission,
        context: TrackerFormWrite<'_>,
    ) -> Result<StoredCustomEntry, FormSubmitError> {
        if route != form.action_id {
            return Err(FormSubmitError::Conflict(
                "Form action does not match the route",
            ));
        }
        let owner = context.owner_id;
        let action = FormAction::parse(route)?;
        let (contract, existing) = form_contract(txn, owner, action).await?;
        if form.schema_hash != contract.hash {
            return Err(FormSubmitError::Conflict(
                "Form contract changed; reload it",
            ));
        }
        let issues = form.issues(
            &parse(&contract.schema)?,
            &parse(&contract.metadata)?,
            &contract.topology,
        )?;
        if !issues.is_empty() {
            return Err(FormSubmitError::Validation(issues));
        }
        let contract_id = contract.action.version_id;
        let mut raw = json!({
            "definition_key":contract.action.key,
            "start":form.start,"end":form.end,"timezone":form.timezone,
            "values":form.values,"units":form.units,
        });
        if let Some(source) = context.source {
            raw["source"] = json!(source);
        }
        let mut evidence = Vec::new();
        if let Some(existing) = &existing {
            let (source, confidence, status, original_text): (String, f64, String, Option<String>) =
                    sqlx::query_as(
                        "SELECT source,confidence,status,original_text FROM events WHERE id=$1 FOR UPDATE",
                    ).bind(existing.id).fetch_one(&mut **txn).await?;
            raw["source"] = json!(source);
            raw["confidence"] = json!(confidence);
            raw["status"] = json!(status);
            raw["original_text"] = json!(original_text);
            evidence = parse(&existing.evidence)?
                .as_array()
                .ok_or(TrackerActionError::InvalidContract)?
                .iter()
                .filter(|reference| reference.get("field_id").is_none())
                .cloned()
                .collect();
        } else {
            let retained: Option<Uuid> = sqlx::query_scalar(
                "SELECT d.id FROM event_definitions d JOIN event_definition_versions v
                      ON v.definition_id=d.id AND v.version=d.current_version
                     WHERE v.id=$1 AND d.owner_id=$2 AND d.status='active' FOR UPDATE OF d",
            )
            .bind(contract_id)
            .bind(owner)
            .fetch_optional(&mut **txn)
            .await?;
            if retained.is_none() {
                return Err(FormSubmitError::Conflict(
                    "Form contract changed; reload it",
                ));
            }
        }
        let bytes = serde_json::to_vec(&raw).map_err(|_| FormSubmitError::Invalid)?;
        let entry =
            CustomEntry::from_json(&bytes, 512_000).map_err(DiaryWriteError::CustomInput)?;
        let stored = if let Some(existing) = existing {
            Self::update_custom_entry_in(
                txn,
                UpdateCustomEntry {
                    owner_id: context.owner_id,
                    id: existing.id,
                    expected_revision: existing.revision,
                    actor: context.actor,
                    entry: &entry,
                    evidence_refs: &evidence,
                },
                entry.display_times(),
            )
            .await?
            .0
        } else {
            let key = context
                .idempotency_key
                .map(str::to_owned)
                .or_else(|| form.idempotency())
                .ok_or(FormSubmitError::Invalid)?;
            Self::create_custom_entry_in(
                txn,
                CreateCustomEntry {
                    owner_id: context.owner_id,
                    actor: context.actor,
                    entry: &entry,
                    idempotency_key: Some(&key),
                    evidence_refs: &evidence,
                },
            )
            .await?
        };
        Ok(stored)
    }
}

pub(crate) struct TrackerFormWrite<'a> {
    pub owner_id: Uuid,
    pub actor: &'a str,
    pub source: Option<&'a str>,
    pub idempotency_key: Option<&'a str>,
}
