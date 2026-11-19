mod model;
pub use model::{TrackerTextPreparation, TrackerTextSession};

use std::{error::Error, fmt, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::{
    access::{Scope, Scopes},
    event_input::known_timezone,
    timestamp::{OffsetTimestamp, StoredInstant},
    tracker_nl::{TrackerRelevance, TrackerTextRequest},
    tracker_selection::is_tracker_proposal,
};
use serde_json::{Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction};
use uuid::Uuid;

use super::{ActionRecord, TrackerActionError, read_locale};
use crate::{
    Database, check_readiness, health_analysis::owner_timezone, share_policy::ShareDestination,
};

#[derive(Debug)]
pub enum TrackerTextError {
    Forbidden,
    Missing,
    Invalid,
    Database,
    Form(super::FormSubmitError),
}
impl fmt::Display for TrackerTextError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Forbidden => "Operation not allowed",
            Self::Missing => "Record not found",
            Self::Invalid => "Invalid arguments",
            Self::Database => "Database unavailable or identity is not ready",
            Self::Form(error) => return error.fmt(f),
        })
    }
}
impl Error for TrackerTextError {}
impl From<sqlx::Error> for TrackerTextError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<TrackerActionError> for TrackerTextError {
    fn from(value: TrackerActionError) -> Self {
        match value {
            TrackerActionError::Missing
            | TrackerActionError::InactiveContract
            | TrackerActionError::EntryChanged => Self::Missing,
            TrackerActionError::InvalidLocale => Self::Invalid,
            _ => Self::Database,
        }
    }
}

pub struct TrackerTextContext<'a> {
    pub owner: Uuid,
    pub scopes: Scopes,
    pub actor: &'a str,
    pub message_at: StoredInstant,
    pub configured_timezone: &'a str,
    pub configured_locale: &'a str,
}

struct RankedTracker {
    score: usize,
    version: Uuid,
}

impl Database {
    pub async fn tracker_text_fallback(
        &self,
        request: &TrackerTextRequest,
        context: TrackerTextContext<'_>,
    ) -> Result<Value, TrackerTextError> {
        match self.prepare_tracker_text(request, context, None).await? {
            TrackerTextPreparation::Complete(value) => Ok(value),
            TrackerTextPreparation::Model(_) => Err(TrackerTextError::Database),
        }
    }

    pub async fn prepare_tracker_text(
        &self,
        request: &TrackerTextRequest,
        context: TrackerTextContext<'_>,
        destination: Option<ShareDestination<'_>>,
    ) -> Result<TrackerTextPreparation, TrackerTextError> {
        let work = async {
            loop {
                // Keep the operation fence across model work without occupying the shared work pool.
                let pool = if destination.is_some() {
                    &self.model_operations
                } else {
                    &self.work
                };
                let mut txn = pool.begin().await?;
                sqlx::query("SET LOCAL statement_timeout = '750ms'")
                    .execute(&mut *txn)
                    .await?;
                check_readiness(&mut txn)
                    .await
                    .map_err(|_| TrackerTextError::Database)?;
                let owner_exists: bool =
                    sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM people WHERE id=$1)")
                        .bind(context.owner)
                        .fetch_one(&mut *txn)
                        .await?;
                if !owner_exists {
                    return Err(TrackerTextError::Database);
                }
                let locale =
                    read_locale(&mut txn, context.owner, None, context.configured_locale).await?;
                let mut timezone = owner_timezone(&mut txn, context.configured_timezone)
                    .await
                    .map_err(|_| TrackerTextError::Database)?;
                if !known_timezone(&timezone) {
                    return Err(TrackerTextError::Invalid);
                }
                let read = context.scopes.permits(&[Scope::ReadDiary]);
                let manage = context.scopes.permits(&[Scope::ManageDefinitions]);
                if (!read && !manage) || (request.selected_event_id.is_some() && !read) {
                    return Err(TrackerTextError::Forbidden);
                }
                let operation = request.operation_key(context.actor);
                let locked: bool =
                    sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(72104623,hashtext($1))")
                        .bind(&operation)
                        .fetch_one(&mut *txn)
                        .await?;
                if !locked {
                    txn.rollback().await?;
                    tokio::time::sleep(Duration::from_millis(25)).await;
                    continue;
                }
                let hashes = request
                    .request_hashes()
                    .map_err(|_| TrackerTextError::Invalid)?;
                let receipt: Option<String> =
                    sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                        .bind(&operation)
                        .fetch_optional(&mut *txn)
                        .await?;
                if let Some(receipt) = receipt {
                    let receipt: Value =
                        serde_json::from_str(&receipt).map_err(|_| TrackerTextError::Database)?;
                    if !receipt["request_hash"]
                        .as_str()
                        .is_some_and(|hash| hashes.iter().any(|wanted| wanted == hash))
                    {
                        return Err(TrackerTextError::Invalid);
                    }
                    let result = receipt
                        .get("result")
                        .cloned()
                        .ok_or(TrackerTextError::Database)?;
                    if result["written"] == true
                        && !context
                            .scopes
                            .permits(&[Scope::ReadDiary, Scope::WriteDiary])
                    {
                        return Err(TrackerTextError::Forbidden);
                    }
                    txn.commit().await?;
                    return Ok(TrackerTextPreparation::Complete(result));
                }
                let (mut candidates, has_more, best) =
                    ranked(&mut txn, context.owner, &request.text).await?;
                if let Some(version) = request.selected_definition_version_id {
                    let active:bool=sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM event_definitions d JOIN event_definition_versions v
                       ON v.definition_id=d.id AND v.version=d.current_version
                     JOIN tracker_configs t ON t.definition_id=d.id
                     WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active' AND v.id=$2)"
                ).bind(context.owner).bind(version).fetch_one(&mut *txn).await?;
                    if !active {
                        return Err(TrackerTextError::Missing);
                    }
                    candidates = vec![version];
                }
                let selected = if let Some(event) = request.selected_event_id {
                    let row = sqlx::query(
                        "SELECT d.key,v.id AS version_id,v.labels::text,t.shortcut,e.revision
                     FROM events e JOIN event_definition_versions v ON v.id=e.definition_version_id
                     JOIN event_definitions d ON d.id=v.definition_id
                     LEFT JOIN tracker_configs t ON t.definition_id=d.id
                     WHERE e.id=$1 AND NOT e.deleted AND d.owner_id=$2 AND d.namespace='user'
                       AND v.allowed_operations ?& ARRAY['update','query']",
                    )
                    .bind(event)
                    .bind(context.owner)
                    .fetch_optional(&mut *txn)
                    .await?
                    .ok_or(TrackerTextError::Missing)?;
                    let record = ActionRecord::from_row(&row)?;
                    let revision: i32 = row.try_get("revision")?;
                    Some(record.action(&locale, Some((event, revision)))?)
                } else {
                    None
                };
                let context_key = operation.replacen("nl-operation:", "nl-context:", 1);
                let pinned: Option<String> =
                    sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                        .bind(&context_key)
                        .fetch_optional(&mut *txn)
                        .await?;
                let mut message_at = context.message_at;
                if let Some(pinned) = pinned {
                    let pinned: Value =
                        serde_json::from_str(&pinned).map_err(|_| TrackerTextError::Database)?;
                    if !pinned["request_hash"]
                        .as_str()
                        .is_some_and(|hash| hashes.iter().any(|wanted| wanted == hash))
                    {
                        return Err(TrackerTextError::Invalid);
                    }
                    message_at = StoredInstant::from_legacy_input(&pinned["message_at"])
                        .ok_or(TrackerTextError::Database)?;
                    timezone = pinned["timezone"]
                        .as_str()
                        .ok_or(TrackerTextError::Database)?
                        .to_owned();
                } else {
                    let value = json!({"request_hash":hashes[0],"message_at":OffsetTimestamp(context.message_at.into()).to_string(),"timezone":timezone});
                    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                        .bind(&context_key)
                        .bind(value.to_string())
                        .execute(&mut *txn)
                        .await?;
                }
                let unselected = request.selected_event_id.is_none()
                    && request.selected_definition_version_id.is_none();
                let selection_required = has_more
                    && best < 10
                    && unselected
                    && !is_tracker_proposal(&request.text)
                        .map_err(|_| TrackerTextError::Database)?;
                let all = has_more && unselected;
                let (forms, selection_has_more) = if read {
                    if let Some(action) = &selected {
                        let form = Self::tracker_generated_form_in(
                            &mut txn,
                            context.owner,
                            action["id"].as_str().ok_or(TrackerTextError::Database)?,
                            &locale,
                        )
                        .await?;
                        (vec![form], false)
                    } else {
                        fallback_forms(&mut txn, context.owner, &locale, &candidates, all).await?
                    }
                } else {
                    (Vec::new(), false)
                };
                let mut result = json!({"schema_version":"tracker.nl.v1","intent":"deterministic_form","reason":if selection_required {"tracker_selection_required"} else {"provider_unavailable"},"forms":forms,"tracker_builder":manage,"written":false});
                if all {
                    result["selection_has_more"] = json!(selection_has_more);
                }
                if let Some(destination) = destination.filter(|_| !selection_required) {
                    return model::prepare(
                        txn,
                        request,
                        context,
                        destination,
                        model::TrackerTextModelInput {
                            locale: &locale,
                            timezone: &timezone,
                            message_at,
                            versions: candidates,
                            selected_action: selected,
                            fallback: result,
                        },
                    )
                    .await;
                }
                txn.commit().await?;
                return Ok(TrackerTextPreparation::Complete(result));
            }
        };
        tokio::time::timeout(Duration::from_millis(2500), work)
            .await
            .map_err(|_| TrackerTextError::Database)?
    }
}

async fn ranked(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    text: &str,
) -> Result<(Vec<Uuid>, bool, usize), TrackerTextError> {
    let relevance = TrackerRelevance::new(text);
    let mut rows=sqlx::query(
        "SELECT d.key,v.id,v.labels::text,v.field_metadata::text,t.shortcut
         FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version
         JOIN tracker_configs t ON t.definition_id=d.id
         WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active' ORDER BY d.key COLLATE \"C\""
    ).bind(owner).fetch(&mut **txn);
    let mut top: Vec<RankedTracker> = Vec::new();
    let mut count = 0usize;
    while let Some(row) = rows.try_next().await? {
        count = count.saturating_add(1);
        let key: String = row.try_get("key")?;
        let shortcut: Option<String> = row.try_get("shortcut")?;
        let labels: Value = serde_json::from_str(&row.try_get::<String, _>("labels")?)
            .map_err(|_| TrackerTextError::Database)?;
        let metadata: Value = serde_json::from_str(&row.try_get::<String, _>("field_metadata")?)
            .map_err(|_| TrackerTextError::Database)?;
        let mut values = vec![key.as_str(), shortcut.as_deref().unwrap_or("")];
        for value in labels
            .as_object()
            .ok_or(TrackerTextError::Database)?
            .values()
        {
            values.push(value.as_str().ok_or(TrackerTextError::Database)?);
        }
        for field in metadata
            .as_object()
            .ok_or(TrackerTextError::Database)?
            .values()
        {
            for value in field["labels"]
                .as_object()
                .ok_or(TrackerTextError::Database)?
                .values()
            {
                values.push(value.as_str().ok_or(TrackerTextError::Database)?);
            }
        }
        let score = relevance.score(values);
        let position = top.partition_point(|candidate| candidate.score >= score);
        if position < 5 {
            top.insert(
                position,
                RankedTracker {
                    score,
                    version: row.try_get("id")?,
                },
            );
            top.truncate(5);
        }
    }
    let best = top.first().map_or(0, |row| row.score);
    Ok((
        top.into_iter().map(|row| row.version).collect(),
        count > 5,
        best,
    ))
}

async fn fallback_forms(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    locale: &str,
    candidates: &[Uuid],
    all: bool,
) -> Result<(Vec<Value>, bool), TrackerTextError> {
    let actions:Vec<Uuid>=sqlx::query_scalar(
        "SELECT v.id FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version
         LEFT JOIN tracker_configs t ON t.definition_id=d.id
         WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active' AND v.allowed_operations ? 'create'
           AND (($2 AND t.id IS NOT NULL) OR (NOT $2 AND v.id=ANY($3))) ORDER BY d.key COLLATE \"C\" LIMIT 51"
    ).bind(owner).bind(all).bind(candidates).fetch_all(&mut **txn).await?;
    let has_more = all && actions.len() > 50;
    let mut forms = Vec::new();
    for version in actions.into_iter().take(50) {
        forms.push(
            Database::tracker_generated_form_in(txn, owner, &format!("create:{version}"), locale)
                .await?,
        );
    }
    Ok((forms, has_more))
}
