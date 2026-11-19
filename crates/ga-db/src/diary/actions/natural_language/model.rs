mod apply;

use ga_domain::{
    access::Scopes,
    event_input::source_offset_datetime,
    timestamp::{OffsetTimestamp, StoredInstant},
    tracker_nl::TrackerTextRequest,
};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

use super::super::localized_label;
use super::{TrackerTextContext, TrackerTextError, fallback_forms};
use crate::share_policy::{self, ShareCategory, ShareDestination, ShareRequirement};

pub enum TrackerTextPreparation {
    Complete(Value),
    Model(Box<TrackerTextSession>),
}

pub struct TrackerTextSession {
    pub(crate) txn: Transaction<'static, Postgres>,
    request: TrackerTextRequest,
    owner: Uuid,
    scopes: Scopes,
    actor: String,
    locale: String,
    timezone: String,
    message_at: StoredInstant,
    destination: String,
    candidates: Vec<Value>,
    selected: Option<Value>,
    pub(crate) fallback: Value,
    pub(crate) source: &'static str,
}

impl TrackerTextSession {
    pub fn prompt(&self) -> Result<String, TrackerTextError> {
        let local = source_offset_datetime(self.message_at, &self.timezone)
            .ok_or(TrackerTextError::Invalid)?;
        serde_json::to_string(&json!({
            "now": OffsetTimestamp(local).to_string(),
            "timezone": self.timezone,
            "text": self.request.text,
            "candidate_trackers": self.candidates,
            "selected_event": self.selected
        }))
        .map_err(|_| TrackerTextError::Database)
    }

    pub fn sharing_requirements(&self) -> Result<Vec<ShareRequirement<'static>>, TrackerTextError> {
        self.candidates
            .iter()
            .map(|candidate| {
                Ok(ShareRequirement {
                    version_id: candidate["definition_version_id"]
                        .as_str()
                        .and_then(|value| value.parse().ok())
                        .ok_or(TrackerTextError::Database)?,
                    categories: categories(candidate),
                })
            })
            .collect()
    }

    pub async fn fallback(self) -> Result<Value, TrackerTextError> {
        tokio::time::timeout(std::time::Duration::from_millis(2500), async {
            self.txn.commit().await?;
            Ok(self.fallback)
        })
        .await
        .map_err(|_| TrackerTextError::Database)?
    }
}

fn categories(candidate: &Value) -> &'static [ShareCategory] {
    if candidate["privacy"] == "sensitive" {
        &[
            ShareCategory::Schema,
            ShareCategory::Facts,
            ShareCategory::OriginalText,
        ]
    } else {
        &[ShareCategory::Schema, ShareCategory::Facts]
    }
}

pub(super) struct TrackerTextModelInput<'a> {
    pub locale: &'a str,
    pub timezone: &'a str,
    pub message_at: StoredInstant,
    pub versions: Vec<Uuid>,
    pub selected_action: Option<Value>,
    pub fallback: Value,
}

pub(super) async fn prepare(
    mut txn: Transaction<'static, Postgres>,
    request: &TrackerTextRequest,
    context: TrackerTextContext<'_>,
    destination: ShareDestination<'_>,
    input: TrackerTextModelInput<'_>,
) -> Result<TrackerTextPreparation, TrackerTextError> {
    let TrackerTextModelInput {
        locale,
        timezone,
        message_at,
        mut versions,
        selected_action,
        mut fallback,
    } = input;
    let selected = if let (Some(event), Some(action)) =
        (request.selected_event_id, selected_action.as_ref())
    {
        let version = action["definition_version_id"]
            .as_str()
            .and_then(|value| value.parse().ok())
            .ok_or(TrackerTextError::Database)?;
        if !versions.contains(&version) {
            versions.truncate(4);
            versions.push(version);
        }
        Some(
            json!({"id":event.to_string(),"revision":action["revision"],"definition_version_id":version.to_string()}),
        )
    } else {
        None
    };
    let mut candidates = Vec::new();
    for &version in &versions {
        candidates.push(projection(&mut txn, context.owner, version, locale).await?);
    }
    let mut shareable = Vec::new();
    let mut first_denied = false;
    let mut selected_denied = false;
    for (index, candidate) in candidates.iter().enumerate() {
        let version = versions[index];
        if share_policy::allows(
            &mut txn,
            ShareRequirement {
                version_id: version,
                categories: categories(candidate),
            },
            destination,
        )
        .await
        .map_err(|_| TrackerTextError::Database)?
        {
            shareable.push(candidate.clone());
        } else {
            first_denied |= index == 0;
            selected_denied |= selected.as_ref().is_some_and(|selected| {
                selected["definition_version_id"] == candidate["definition_version_id"]
            });
        }
    }
    if first_denied || selected_denied {
        fallback["reason"] = json!("sensitive_tracker_consent_required");
        fallback
            .as_object_mut()
            .ok_or(TrackerTextError::Database)?
            .remove("selection_has_more");
        if selected_action.is_none()
            && context
                .scopes
                .permits(&[ga_domain::access::Scope::ReadDiary])
        {
            fallback["forms"] = json!(
                fallback_forms(&mut txn, context.owner, locale, &versions, false)
                    .await?
                    .0
            );
        }
        txn.commit().await?;
        return Ok(TrackerTextPreparation::Complete(fallback));
    }
    if fallback.get("selection_has_more").is_none()
        && selected_action.is_none()
        && context
            .scopes
            .permits(&[ga_domain::access::Scope::ReadDiary])
    {
        let versions: Vec<Uuid> = shareable
            .iter()
            .map(|candidate| {
                candidate["definition_version_id"]
                    .as_str()
                    .and_then(|value| value.parse().ok())
                    .ok_or(TrackerTextError::Database)
            })
            .collect::<Result<_, _>>()?;
        fallback["forms"] = json!(
            fallback_forms(&mut txn, context.owner, locale, &versions, false)
                .await?
                .0
        );
    }
    Ok(TrackerTextPreparation::Model(Box::new(
        TrackerTextSession {
            txn,
            request: request.clone(),
            owner: context.owner,
            scopes: context.scopes,
            actor: context.actor.to_owned(),
            locale: locale.to_owned(),
            timezone: timezone.to_owned(),
            message_at,
            destination: destination.instance().to_owned(),
            candidates: shareable,
            selected,
            fallback,
            source: "manual",
        },
    )))
}

async fn projection(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    version: Uuid,
    locale: &str,
) -> Result<Value, TrackerTextError> {
    let row = sqlx::query(
        "SELECT d.id AS definition_id,d.key,v.id,v.version,v.labels::text,t.shortcut,v.topology,v.privacy,
                v.schema_hash,v.schema::text,v.field_metadata::text
         FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id
         LEFT JOIN tracker_configs t ON t.definition_id=d.id
         WHERE v.id=$1 AND d.owner_id=$2 AND d.namespace='user'"
    ).bind(version).bind(owner).fetch_optional(&mut **txn).await?.ok_or(TrackerTextError::Missing)?;
    let labels: Value = serde_json::from_str(&row.try_get::<String, _>("labels")?)
        .map_err(|_| TrackerTextError::Database)?;
    let schema: Value = serde_json::from_str(&row.try_get::<String, _>("schema")?)
        .map_err(|_| TrackerTextError::Database)?;
    let metadata: Value = serde_json::from_str(&row.try_get::<String, _>("field_metadata")?)
        .map_err(|_| TrackerTextError::Database)?;
    let mut fields = Vec::new();
    for (name, field) in metadata.as_object().ok_or(TrackerTextError::Database)? {
        fields.push(json!({
            "name": name,
            "field_id": field["id"],
            "label": localized_label(&field["labels"], locale)?,
            "semantic": field["semantic"],
            "unit": field.get("unit"),
            "schema": schema["properties"][name]
        }));
    }
    Ok(json!({
        "definition_id": row.try_get::<Uuid, _>("definition_id")?.to_string(),
        "definition_key": row.try_get::<String, _>("key")?,
        "definition_version_id": version.to_string(),
        "version": row.try_get::<i32, _>("version")?,
        "label": localized_label(&labels, locale)?,
        "shortcut": row.try_get::<Option<String>, _>("shortcut")?,
        "topology": row.try_get::<String, _>("topology")?,
        "privacy": row.try_get::<String, _>("privacy")?,
        "schema_hash": row.try_get::<String, _>("schema_hash")?,
        "fields": fields
    }))
}
