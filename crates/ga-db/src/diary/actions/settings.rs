use std::time::Duration;

use ga_domain::event_schema::TrackerSettingsUpdate;
use serde_json::{Value, json};
use uuid::Uuid;

use super::TrackerSetupStoreError;
use crate::{Database, diary::ManageCustomDefinition, share_policy::OWNER_ONBOARDING_LOCK};

const TRACKER_RULE_NAMESPACE: Uuid = Uuid::from_u128(0x68af2e31_b719_45a1_a934_92ae9c92091b);
type SavedSettings = (i32, Option<String>, bool, Option<String>, Option<String>);

impl Database {
    /// Replace one owner's settings with revision checks and retire superseded reminders.
    pub async fn update_tracker_settings(
        &self,
        actor: ManageCustomDefinition<'_>,
        tracker_id: Uuid,
        settings: &TrackerSettingsUpdate,
    ) -> Result<Value, TrackerSetupStoreError> {
        tokio::time::timeout(Duration::from_millis(1800), async {
            let mut txn = self.begin_definition_management(actor).await?;
            let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                .bind(OWNER_ONBOARDING_LOCK).fetch_one(&mut *txn).await?;
            if !locked {
                return Err(TrackerSetupStoreError::Database);
            }
            let current: SavedSettings = sqlx::query_as(
                "SELECT revision,shortcut,reminder_enabled,reminder_time,reminder_timezone
                 FROM tracker_configs WHERE id=$1 AND owner_id=$2 FOR UPDATE",
            ).bind(tracker_id).bind(actor.owner_id).fetch_optional(&mut *txn).await?
                .ok_or(TrackerSetupStoreError::TrackerMissing)?;
            if settings.revision.as_i64() != Some(i64::from(current.0)) {
                return Err(TrackerSetupStoreError::SettingsChanged);
            }
            let changed = current.1 != settings.shortcut
                || current.2 != settings.reminder_enabled
                || current.3 != settings.reminder_time
                || current.4.as_deref() != Some(settings.reminder_timezone.as_str());
            let revision: i32 = sqlx::query_scalar(
                "UPDATE tracker_configs SET shortcut=$2,reminder_enabled=$3,reminder_time=$4,
                    reminder_timezone=$5,revision=revision+$6,
                    updated_at=CASE WHEN $6=1 THEN now() ELSE updated_at END
                 WHERE id=$1 RETURNING revision",
            ).bind(tracker_id).bind(&settings.shortcut).bind(settings.reminder_enabled)
                .bind(&settings.reminder_time).bind(&settings.reminder_timezone).bind(i32::from(changed))
                .fetch_one(&mut *txn).await?;
            if changed {
                let rule = Uuid::new_v5(&TRACKER_RULE_NAMESPACE, tracker_id.to_string().as_bytes());
                sqlx::query(
                    "UPDATE outbox_messages SET state='cancelled',next_attempt_at=NULL,updated_at=now()
                     WHERE state='queued' AND intent->'evidence_refs' @> $1::jsonb",
                ).bind(json!([format!("rule:{rule}")]).to_string()).execute(&mut *txn).await?;
                sqlx::query(
                    "UPDATE pending_questions SET status='cancelled' WHERE kind='tracker_reminder'
                     AND evidence->>'tracker_id'=$1 AND status IN ('pending','sending','uncertain')",
                ).bind(tracker_id.to_string()).execute(&mut *txn).await?;
            }
            txn.commit().await?;
            Ok(json!({"id":tracker_id.to_string(),"revision":revision,"shortcut":settings.shortcut,
                "reminder_enabled":settings.reminder_enabled,"reminder_time":settings.reminder_time,
                "reminder_timezone":settings.reminder_timezone}))
        }).await.map_err(|_|TrackerSetupStoreError::Database)?
    }
}
