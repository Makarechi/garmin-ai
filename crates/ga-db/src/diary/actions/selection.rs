use futures_util::TryStreamExt;
use ga_domain::tracker_selection::{TrackerLabel, TrackerSelection};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{ActionRecord, TrackerActionError};
use crate::Database;

impl Database {
    /// Apply schema consent before ranking, retaining at most five equal best matches.
    pub(crate) async fn select_tracker_actions_in(
        txn: &mut Transaction<'_, Postgres>,
        owner: Uuid,
        locale: &str,
        destination: &str,
        selection: &TrackerSelection,
    ) -> Result<Vec<Value>, TrackerActionError> {
        let query = concat!(
            "WITH destination(kind,instance,categories) AS (VALUES ('channel'::text,$2::text,'[\"schema\"]'::jsonb))
             SELECT d.key,version.id AS version_id,version.labels::text,t.shortcut
             FROM event_definitions d JOIN event_definition_versions AS version
               ON version.definition_id=d.id AND version.version=d.current_version
             CROSS JOIN destination
             LEFT JOIN tracker_configs t ON t.definition_id=d.id
             LEFT JOIN app_state AS consent ON consent.key=concat(
               'tracker-consent:',version.definition_id,':',destination.kind,':',destination.instance)
             WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active'
               AND version.allowed_operations ? 'create'
               AND (version.privacy='private' OR (",
            include_str!("../../share_policy/consent.sql"),
            ")) ORDER BY d.key COLLATE \"C\""
        );
        let mut rows = sqlx::query_as::<_, ActionRecord>(query)
            .bind(owner)
            .bind(destination)
            .fetch(&mut **txn);
        let mut best = 0;
        let mut selected = Vec::new();
        while let Some(row) = rows.try_next().await? {
            let action = row.action(locale, None)?;
            let label = TrackerLabel::new(
                action["label"]
                    .as_str()
                    .ok_or(TrackerActionError::InvalidContract)?,
            );
            if let Some(score) = selection
                .score(&label)
                .map_err(|_| TrackerActionError::InvalidContract)?
            {
                if score > best {
                    selected.clear();
                    best = score;
                }
                if score == best && selected.len() < 5 {
                    selected.push(action);
                }
            }
        }
        Ok(selected)
    }

    pub(crate) async fn tracker_create_action_active_in(
        txn: &mut Transaction<'_, Postgres>,
        owner: Uuid,
        action: &Value,
    ) -> Result<bool, sqlx::Error> {
        let version = action["definition_version_id"]
            .as_str()
            .and_then(|id| id.parse::<Uuid>().ok());
        let Some(version) = version else {
            return Ok(false);
        };
        if action["id"].as_str() != Some(format!("create:{version}").as_str()) {
            return Ok(false);
        }
        sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM event_definitions d JOIN event_definition_versions v
               ON v.definition_id=d.id AND v.version=d.current_version
             WHERE d.owner_id=$1 AND d.namespace='user' AND d.status='active'
               AND v.id=$2 AND v.allowed_operations ? 'create')",
        )
        .bind(owner)
        .bind(version)
        .fetch_one(&mut **txn)
        .await
    }
}
