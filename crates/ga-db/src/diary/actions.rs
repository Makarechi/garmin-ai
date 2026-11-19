mod community;
mod forms;
mod pack_export;
mod settings;
mod setup;

pub use community::{CommunityPackError, community_pack_catalog, parse_community_pack};
pub(crate) use forms::TrackerFormWrite;
pub use forms::{FileImportOutcome, FileImportStoreError};
pub use forms::{FormSubmission, FormSubmitError};
pub use setup::TrackerSetupStoreError;

use std::{error::Error, fmt, time::Duration};

use ga_domain::language_tags::normalize_language_tag;
use serde_json::{Map, Value, json};
use sqlx::{FromRow, Postgres, Row, Transaction, postgres::PgRow};
use uuid::Uuid;

use super::DiaryReadError;
use crate::Database;

#[derive(Debug)]
pub enum TrackerActionError {
    Diary(DiaryReadError),
    Missing,
    InactiveContract,
    EntryChanged,
    InvalidLocale,
    InvalidContract,
}

impl fmt::Display for TrackerActionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Diary(error) => error.fmt(f),
            Self::Missing => f.write_str("Record not found"),
            Self::InactiveContract => f.write_str("Form contract is no longer active; reload it"),
            Self::EntryChanged => f.write_str("Entry changed; reload its form"),
            Self::InvalidLocale => f.write_str("Invalid language tag"),
            Self::InvalidContract => f.write_str("Recorded tracker form is unavailable"),
        }
    }
}
impl Error for TrackerActionError {}
impl From<DiaryReadError> for TrackerActionError {
    fn from(value: DiaryReadError) -> Self {
        Self::Diary(value)
    }
}
impl From<sqlx::Error> for TrackerActionError {
    fn from(_: sqlx::Error) -> Self {
        Self::Diary(DiaryReadError::Database)
    }
}

struct ActionRecord {
    key: String,
    version_id: Uuid,
    labels: String,
    shortcut: Option<String>,
}

impl FromRow<'_, PgRow> for ActionRecord {
    fn from_row(row: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            key: row.try_get("key")?,
            version_id: row.try_get("version_id")?,
            labels: row.try_get("labels")?,
            shortcut: row.try_get("shortcut")?,
        })
    }
}

impl ActionRecord {
    fn action(&self, locale: &str, edit: Option<(Uuid, i32)>) -> Result<Value, TrackerActionError> {
        let labels: Value =
            serde_json::from_str(&self.labels).map_err(|_| TrackerActionError::InvalidContract)?;
        let label = match self.shortcut.as_deref().filter(|label| !label.is_empty()) {
            Some(shortcut) => shortcut,
            None => localized_label(&labels, locale)?,
        };
        let (id, kind) = match edit {
            Some((event, revision)) => (format!("edit:{event}:{revision}"), "edit_entry"),
            None => (format!("create:{}", self.version_id), "create_entry"),
        };
        Ok(json!({
            "id":id,"kind":kind,"label":label,"definition_key":self.key,
            "definition_version_id":self.version_id.to_string(),"form_id":id,
            "event_id":edit.map(|(id,_)| id.to_string()),
            "revision":edit.map(|(_, revision)| revision),
        }))
    }
}

impl Database {
    pub async fn available_tracker_actions(
        &self,
        owner: Uuid,
        locale: Option<&str>,
        configured_locale: &str,
    ) -> Result<Value, TrackerActionError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let locale = read_locale(&mut txn, owner, locale, configured_locale).await?;
            let rows: Vec<ActionRecord> = sqlx::query_as(
                "SELECT d.key, v.id AS version_id, v.labels::text, t.shortcut
                 FROM event_definitions d JOIN event_definition_versions v
                   ON v.definition_id=d.id AND v.version=d.current_version
                 LEFT JOIN tracker_configs t ON t.definition_id=d.id
                 WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active'
                   AND v.allowed_operations ? 'create' ORDER BY d.key",
            )
            .bind(owner)
            .fetch_all(&mut *txn)
            .await?;
            let actions = rows
                .into_iter()
                .map(|record| record.action(&locale, None))
                .collect::<Result<Vec<_>, _>>()?;
            txn.commit().await?;
            Ok(json!({"actions":actions}))
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }

    pub async fn tracker_action_for_event(
        &self,
        owner: Uuid,
        event: Uuid,
        locale: Option<&str>,
        configured_locale: &str,
    ) -> Result<Value, TrackerActionError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let locale = read_locale(&mut txn, owner, locale, configured_locale).await?;
            let (version, revision): (Option<Uuid>, i32) = sqlx::query_as(
                "SELECT definition_version_id,revision FROM events WHERE id=$1 AND NOT deleted",
            )
            .bind(event)
            .fetch_optional(&mut *txn)
            .await?
            .ok_or(TrackerActionError::Missing)?;
            let version = version.ok_or(TrackerActionError::Missing)?;
            let row: ActionRecord = sqlx::query_as(
                "SELECT d.key, v.id AS version_id, v.labels::text, t.shortcut
                 FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id
                 LEFT JOIN tracker_configs t ON t.definition_id=d.id
                 WHERE v.id=$1 AND d.namespace='user' AND d.owner_id=$2
                   AND v.allowed_operations ?& ARRAY['update','query']",
            )
            .bind(version)
            .bind(owner)
            .fetch_optional(&mut *txn)
            .await?
            .ok_or(TrackerActionError::Missing)?;
            let action = row.action(&locale, Some((event, revision)))?;
            txn.commit().await?;
            Ok(action)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }
}

async fn read_locale(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    explicit: Option<&str>,
    configured: &str,
) -> Result<String, TrackerActionError> {
    let saved: Option<String> = sqlx::query_scalar(
        "SELECT locale FROM people WHERE singleton AND id=$1
         AND EXISTS(SELECT 1 FROM app_state WHERE key='preferences:onboarding')",
    )
    .bind(owner)
    .fetch_optional(&mut **txn)
    .await?;
    normalize_language_tag(explicit.or(saved.as_deref()).unwrap_or(configured))
        .ok_or(TrackerActionError::InvalidLocale)
}

fn localized_label<'a>(labels: &'a Value, locale: &str) -> Result<&'a str, TrackerActionError> {
    let labels: &Map<String, Value> = labels
        .as_object()
        .ok_or(TrackerActionError::InvalidContract)?;
    let mut current = locale;
    loop {
        if let Some(label) = labels
            .get(current)
            .and_then(Value::as_str)
            .filter(|label| !label.is_empty())
        {
            return Ok(label);
        }
        let Some((parent, _)) = current.rsplit_once('-') else {
            break;
        };
        let last_length = parent.rsplit('-').next().map_or(0, str::len);
        current = if last_length == 1 {
            parent.rsplit_once('-').map_or("", |(ancestor, _)| ancestor)
        } else {
            parent
        };
        if current.is_empty() {
            break;
        }
    }
    labels
        .get("en")
        .and_then(Value::as_str)
        .filter(|label| !label.is_empty())
        .or_else(|| {
            labels
                .iter()
                .min_by_key(|(tag, _)| (tag.len(), tag.as_str()))
                .and_then(|(_, label)| label.as_str())
        })
        .ok_or(TrackerActionError::InvalidContract)
}
