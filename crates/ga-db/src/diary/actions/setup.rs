use std::{error::Error, fmt, time::Duration};

use ga_domain::{event_schema::TrackerSetupDraft, timestamp::StoredInstant};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{ActionRecord, TrackerActionError, forms::projection};
use crate::{
    Database,
    diary::{CustomRegistryError, ManageCustomDefinition},
};

#[derive(Debug)]
pub enum TrackerSetupStoreError {
    Registry(CustomRegistryError),
    Contract(TrackerActionError),
    PreviewChanged,
    Database,
}

impl fmt::Display for TrackerSetupStoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Registry(error) => error.fmt(f),
            Self::Contract(error) => error.fmt(f),
            Self::PreviewChanged => f.write_str("Tracker preview changed; preview it again"),
            Self::Database => f.write_str("Tracker setup could not be saved"),
        }
    }
}
impl Error for TrackerSetupStoreError {}
impl From<CustomRegistryError> for TrackerSetupStoreError {
    fn from(error: CustomRegistryError) -> Self {
        Self::Registry(error)
    }
}
impl From<TrackerActionError> for TrackerSetupStoreError {
    fn from(error: TrackerActionError) -> Self {
        Self::Contract(error)
    }
}
impl From<sqlx::Error> for TrackerSetupStoreError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Save a short-lived digest of the draft without activating a tracker.
    pub async fn preview_tracker_setup(
        &self,
        request: ManageCustomDefinition<'_>,
        draft: &TrackerSetupDraft,
    ) -> Result<Value, TrackerSetupStoreError> {
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let document = draft.document();
        let definition = draft.definition().contract();
        let locale = document["locale"]
            .as_str()
            .ok_or(TrackerSetupStoreError::Database)?;
        let mut projected =
            projection::fields(&definition["schema"], &definition["fields"], locale)?;
        let order = document["fields"]
            .as_array()
            .ok_or(TrackerSetupStoreError::Database)?;
        projected.sort_by_key(|field| {
            order
                .iter()
                .position(|draft| draft["key"] == field["name"])
                .unwrap_or(order.len())
        });
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_definition_management(request).await?;
            let now = setup_clock(&mut txn).await?;
            let expires = now.checked_add(time::Duration::minutes(15)).ok_or(TrackerSetupStoreError::Database)?;
            let encoded = |stamp: OffsetDateTime| StoredInstant::try_from(stamp)
                .map(|stamp| stamp.to_string().replace('Z',"+00:00"))
                .map_err(|_|TrackerSetupStoreError::Database);
            sqlx::query("DELETE FROM app_state WHERE key LIKE 'tracker-preview:%' AND value->>'expires_at'<$1")
                .bind(encoded(now)?).execute(&mut *txn).await?;
            sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                .bind(format!("tracker-preview:{token}")).bind(json!({"draft_hash":draft.hash(),"expires_at":encoded(expires)?}).to_string())
                .execute(&mut *txn).await?;
            txn.commit().await?;
            Ok(json!({"confirmation_token":token,"definition":definition,"form":{
                "title":document["name"],"topology":document["topology"],"fields":projected,
            },"settings":{"shortcut":document["shortcut"],"reminder_enabled":document["reminder_enabled"],
                "reminder_time":document["reminder_time"],"reminder_timezone":document["reminder_timezone"]}}))
        }).await.map_err(|_|TrackerSetupStoreError::Database)?
    }

    /// Recheck the exact preview and activate the definition, tracker and metric bindings atomically.
    pub async fn confirm_tracker_setup(
        &self,
        request: ManageCustomDefinition<'_>,
        draft: &TrackerSetupDraft,
        token: &str,
    ) -> Result<Value, TrackerSetupStoreError> {
        if token.len() != 64
            || !token
                .bytes()
                .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
        {
            return Err(TrackerSetupStoreError::PreviewChanged);
        }
        tokio::time::timeout(Duration::from_millis(2500), async {
            let mut txn = self.begin_definition_management(request).await?;
            let key = format!("tracker-preview:{token}");
            let recorded: Option<String> =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1 FOR UPDATE")
                    .bind(&key)
                    .fetch_optional(&mut *txn)
                    .await?;
            let now = StoredInstant::try_from(setup_clock(&mut txn).await?)
                .map_err(|_| TrackerSetupStoreError::Database)?;
            let current = recorded.and_then(|value| serde_json::from_str::<Value>(&value).ok());
            if !current.is_some_and(|current| {
                current["draft_hash"] == draft.hash()
                    && StoredInstant::from_legacy_input(&current["expires_at"])
                        .is_some_and(|deadline| deadline > now)
            }) {
                return Err(TrackerSetupStoreError::PreviewChanged);
            }
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(key)
                .execute(&mut *txn)
                .await?;
            let created = Self::create_tracker_in(&mut txn, request, draft).await?;
            txn.commit().await?;
            Ok(created)
        })
        .await
        .map_err(|_| TrackerSetupStoreError::Database)?
    }

    pub(super) async fn create_tracker_in(
        txn: &mut Transaction<'_, Postgres>,
        request: ManageCustomDefinition<'_>,
        draft: &TrackerSetupDraft,
    ) -> Result<Value, TrackerSetupStoreError> {
        let definition = Self::draft_custom_definition_in(txn, request, draft.definition()).await?;
        let tracker = Uuid::new_v4();
        let document = draft.document();
        let shortcut = document["shortcut"]
            .as_str()
            .filter(|value| !value.is_empty())
            .or_else(|| document["name"].as_str());
        sqlx::query(
            "INSERT INTO tracker_configs(id,owner_id,definition_id,shortcut,reminder_enabled,
                reminder_time,reminder_timezone,settings,revision)
             VALUES($1,$2,$3,$4,$5,$6,$7,'{}',1)",
        )
        .bind(tracker)
        .bind(request.owner_id)
        .bind(definition.id)
        .bind(shortcut)
        .bind(document["reminder_enabled"].as_bool())
        .bind(document["reminder_time"].as_str())
        .bind(document["reminder_timezone"].as_str())
        .execute(&mut **txn)
        .await?;
        let version =
            Self::activate_custom_definition_in(txn, request, definition.id, definition.revision)
                .await?;
        let action = ActionRecord {
            key: definition.key.clone(),
            version_id: version.id,
            labels: draft.definition().contract()["labels"].to_string(),
            shortcut: shortcut.map(str::to_owned),
        }
        .action(
            document["locale"]
                .as_str()
                .ok_or(TrackerSetupStoreError::Database)?,
            None,
        )?;
        Ok(json!({
            "tracker": {
                "id": tracker.to_string(),
                "definition_id": definition.id.to_string(),
                "definition_key": definition.key,
                "revision": 1,
                "shortcut": shortcut,
                "reminder_enabled": document["reminder_enabled"],
                "reminder_time": document["reminder_time"],
                "reminder_timezone": document["reminder_timezone"],
            },
            "action": action,
        }))
    }
}

async fn setup_clock(txn: &mut Transaction<'_, Postgres>) -> Result<OffsetDateTime, sqlx::Error> {
    sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await
}
