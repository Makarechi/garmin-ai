use std::{collections::BTreeSet, time::Duration};

use serde_json::{Value, json};
use uuid::Uuid;

use super::TrackerActionError;
use crate::Database;

impl Database {
    /// Export definition contracts and optional tracker settings in a consistent read snapshot.
    pub async fn export_tracker_pack(
        &self,
        owner_id: Uuid,
        definitions: &[Uuid],
    ) -> Result<Value, TrackerActionError> {
        if !(1..=32).contains(&definitions.len()) {
            return Err(TrackerActionError::InvalidContract);
        }
        let selected = definitions.iter().copied().collect::<BTreeSet<_>>();
        tokio::time::timeout(Duration::from_secs(4), async {
            let mut txn = self.begin_diary_read().await?;
            let rows: Vec<(Uuid, String, String, Option<i32>)> = sqlx::query_as(
                "SELECT id,key,status,current_version FROM event_definitions
                 WHERE id=ANY($1) AND namespace='user' AND owner_id=$2 ORDER BY key",
            ).bind(definitions).bind(owner_id).fetch_all(&mut *txn).await?;
            if rows.len() != selected.len() {
                return Err(TrackerActionError::Missing);
            }
            let mut exported = Vec::with_capacity(rows.len());
            for (id, key, status, current_version) in rows {
                let versions: Vec<String> = sqlx::query_scalar(
                    "SELECT jsonb_build_object('version',version,'schema',schema,'schema_hash',schema_hash,
                         'field_metadata',field_metadata,'topology',topology,'labels',labels,'privacy',privacy,
                         'allowed_operations',allowed_operations)::text
                     FROM event_definition_versions WHERE definition_id=$1 ORDER BY version",
                ).bind(id).fetch_all(&mut *txn).await?;
                let versions = versions.iter().map(|wire|decode(wire)).collect::<Result<Vec<_>,_>>()?;
                let tracker: Option<String> = sqlx::query_scalar(
                    "SELECT jsonb_build_object('shortcut',shortcut,'reminder_enabled',reminder_enabled,
                        'reminder_time',reminder_time,'reminder_timezone',reminder_timezone)::text
                     FROM tracker_configs WHERE definition_id=$1 AND owner_id=$2",
                ).bind(id).bind(owner_id).fetch_optional(&mut *txn).await?;
                let mappings: Vec<(String, String)> = sqlx::query_as(
                    "SELECT jsonb_build_object('key',d.key,'version',v.version,'value_kind',v.value_kind,
                            'unit',v.unit,'dimension',v.dimension,'scale_id',v.scale_id,'scale_version',v.scale_version,
                            'aggregation',v.aggregation,'coverage_policy',v.coverage_policy,'time_semantics',v.time_semantics,
                            'minimum',v.minimum,'maximum',v.maximum,'labels',v.labels,'allowed_methods',v.allowed_methods,
                            'category_domain',v.category_domain,'schema_hash',v.schema_hash)::text,
                        jsonb_build_object('event_definition_version',e.version,'field_id',m.field_id,
                            'metric_key',d.key,'metric_version',v.version,'projection_version',m.projection_version)::text
                     FROM event_metric_mappings m
                       JOIN event_definition_versions e ON e.id=m.event_definition_version_id
                       JOIN metric_definition_versions v ON v.id=m.metric_definition_version_id
                       JOIN metric_definitions d ON d.id=v.definition_id WHERE e.definition_id=$1",
                ).bind(id).fetch_all(&mut *txn).await?;
                let mut metrics = Vec::with_capacity(mappings.len());
                let mut links = Vec::with_capacity(mappings.len());
                for (metric, mapping) in mappings {
                    metrics.push(decode(&metric)?);
                    links.push(decode(&mapping)?);
                }
                exported.push(json!({"key":key,"status":status,"current_version":current_version,
                    "versions":versions,"tracker":tracker.as_deref().map(decode).transpose()?,
                    "metrics":metrics,"mappings":links}));
            }
            txn.commit().await?;
            Ok(json!({"format":"garmin-ai-tracker-pack-v1","trackers":exported}))
        }).await.map_err(|_|TrackerActionError::InvalidContract)?
    }
}

fn decode(wire: &str) -> Result<Value, TrackerActionError> {
    serde_json::from_str(wire).map_err(|_| TrackerActionError::InvalidContract)
}
