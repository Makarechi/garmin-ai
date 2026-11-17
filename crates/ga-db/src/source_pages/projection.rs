use ga_domain::{
    legacy_json::{Utf8Layout, encode},
    sha256_hex,
    source_contract::{SourceOperation, SourceRecord, SourceTimeSemantics, SourceTimestamp},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{RawRevision, SourcePageError, read_state, valid_instance, write_state};

const MAPPING_LIMIT: i64 = 1000;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct MappingDraft {
    source_instance_id: String,
    metric_version_id: String,
    value_path: Vec<String>,
    unit_path: Vec<String>,
    unit: String,
}

pub(super) struct ConfirmedMapping {
    state_key: String,
    mapping_id: String,
    mapping_hash: String,
    draft: MappingDraft,
    metric_name: String,
    metric_version: Uuid,
    min: f64,
    max: f64,
}

pub(super) async fn confirmed_mappings(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    instance: &str,
    configured_source_hash: &str,
) -> Result<Vec<ConfirmedMapping>, SourcePageError> {
    let rows = sqlx::query_as::<_, (String, String)>(
        "SELECT key,value::text FROM app_state
         WHERE starts_with(key,'source-metric-mapping:')
           AND value->>'owner_id'=$1
           AND value->'mapping'->>'source_instance_id'=$2
           AND value->>'state' IN ('confirmed_not_normalized','normalized')
         ORDER BY key LIMIT $3",
    )
    .bind(owner.to_string())
    .bind(instance)
    .bind(MAPPING_LIMIT + 1)
    .fetch_all(&mut **txn)
    .await?;
    if rows.len() as i64 > MAPPING_LIMIT {
        return Err(SourcePageError::Mapping);
    }
    let mut result = Vec::new();
    for (key, raw) in rows {
        let state = serde_json::from_str::<Value>(&raw).map_err(|_| SourcePageError::Mapping)?;
        if state["source_hash"] != configured_source_hash {
            continue;
        }
        let Ok(mut draft) = serde_json::from_value::<MappingDraft>(state["mapping"].clone()) else {
            continue;
        };
        let Ok(version) = Uuid::parse_str(&draft.metric_version_id) else {
            continue;
        };
        draft.metric_version_id = version.to_string();
        if !valid_draft(&draft) {
            continue;
        }
        let linked: Option<Uuid> = sqlx::query_scalar(
            "SELECT definition.id FROM event_metric_mappings AS binding
             JOIN event_definition_versions AS version ON version.id=binding.event_definition_version_id
             JOIN event_definitions AS definition ON definition.id=version.definition_id
             WHERE binding.metric_definition_version_id=$1 AND definition.owner_id=$2
               AND definition.status='active' AND version.version=definition.current_version
             ORDER BY definition.id LIMIT 1 FOR UPDATE OF definition NOWAIT",
        ).bind(version).bind(owner).fetch_optional(&mut **txn).await?;
        if linked.is_none() {
            continue;
        }
        let metric = sqlx::query_as::<_, (String, f64, f64)>(
            "SELECT metric.key, version.minimum, version.maximum FROM metric_definitions AS metric
             JOIN metric_definition_versions AS version ON version.definition_id=metric.id
             WHERE version.id=$1 AND metric.owner_id=$2 AND metric.namespace='user'
               AND metric.status='active' AND metric.current_version=version.version
               AND version.value_kind='physical_number' AND version.time_semantics='point'
               AND version.unit=$3 AND version.minimum IS NOT NULL AND version.maximum IS NOT NULL
               AND $4::jsonb = jsonb_build_object(
                 'id',metric.key,'version_id',version.id::text,'schema_hash',version.schema_hash,
                 'labels',version.labels,'value_kind',version.value_kind,'dimension',version.dimension,
                 'unit',version.unit,'minimum',version.minimum,'maximum',version.maximum,
                 'time_semantics',version.time_semantics)
             FOR UPDATE OF metric NOWAIT",
        ).bind(version).bind(owner).bind(&draft.unit).bind(state["metric"].to_string())
            .fetch_optional(&mut **txn).await?;
        let Some((name, min, max)) = metric else {
            continue;
        };
        let id = state["id"]
            .as_str()
            .filter(|id| super::valid_sha256(id))
            .ok_or(SourcePageError::Mapping)?;
        let serialized = serde_json::to_vec(&draft).map_err(|_| SourcePageError::Mapping)?;
        let canonical = encode(&serialized, Utf8Layout::Compact, serialized.len())
            .map_err(|_| SourcePageError::Mapping)?;
        result.push(ConfirmedMapping {
            state_key: key,
            mapping_id: id.to_owned(),
            mapping_hash: sha256_hex(&canonical),
            draft,
            metric_name: name,
            metric_version: version,
            min,
            max,
        });
    }
    Ok(result)
}

fn valid_draft(draft: &MappingDraft) -> bool {
    let path_ok = |path: &[String]| {
        (1..=8).contains(&path.len())
            && path.iter().all(|key| {
                (1..=100).contains(&key.chars().count())
                    && key.bytes().all(|byte| byte >= 0x20 && byte != 0x7f)
            })
    };
    let prefix = draft.value_path.len().min(draft.unit_path.len());
    valid_instance(&draft.source_instance_id)
        && (1..=32).contains(&draft.unit.len())
        && draft
            .unit
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_%./-".contains(&byte))
        && path_ok(&draft.value_path)
        && path_ok(&draft.unit_path)
        && draft.value_path[..prefix] != draft.unit_path[..prefix]
}

pub(super) async fn project_record(
    txn: &mut Transaction<'_, Postgres>,
    instance: &str,
    mappings: &[ConfirmedMapping],
    record: &SourceRecord,
    raw: &RawRevision,
    fetched: SourceTimestamp,
    time_semantics: SourceTimeSemantics,
) -> Result<bool, SourcePageError> {
    if mappings.is_empty() {
        return Ok(false);
    }
    let payload = record.payload()?;
    let semantics = match time_semantics {
        SourceTimeSemantics::Instant => "instant",
        SourceTimeSemantics::Interval => "interval",
        SourceTimeSemantics::CalendarDay => "calendar_day",
    };
    let may_project = record.operation == SourceOperation::Upsert
        && time_semantics == SourceTimeSemantics::Instant
        && record.effective_end.is_none();
    let mut changed = false;
    for mapping in mappings {
        let key = format!(
            "source-metric-projection:{}:{}:{}",
            mapping.mapping_id, mapping.mapping_hash, raw.identity
        );
        let previous = read_state(txn, &key).await?;
        let previous = previous.as_ref().unwrap_or(&Value::Null);
        if previous["revision"].as_i64() == Some(raw.revision)
            && previous["time_semantics"] == semantics
        {
            continue;
        }
        let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut **txn)
            .await?;
        if let Some(previous_id) = previous["observation_id"].as_str() {
            let previous_id = Uuid::parse_str(previous_id).map_err(|_| SourcePageError::Mapping)?;
            changed |= sqlx::query(
                "UPDATE metric_observations SET valid=false,invalidated_at=$2 WHERE id=$1 AND valid",
            ).bind(previous_id).bind(at).execute(&mut **txn).await?.rows_affected() > 0;
        }
        let generation = previous["generation"]
            .as_i64()
            .unwrap_or_default()
            .checked_add(1)
            .ok_or(SourcePageError::Mapping)?;
        let value = may_project
            .then(|| {
                payload.numeric_field(
                    &mapping.draft.value_path,
                    &mapping.draft.unit_path,
                    &mapping.draft.unit,
                )
            })
            .flatten()
            .filter(|value| (mapping.min..=mapping.max).contains(value));
        let mut observation = None;
        let status = if let Some(value) = value {
            observation = Some(
                insert_observation(
                    txn,
                    Observation {
                        instance,
                        mapping,
                        record,
                        raw,
                        fetched,
                        at,
                        generation,
                        value,
                    },
                )
                .await?,
            );
            sqlx::query(
                "UPDATE app_state SET value=value || '{\"state\":\"normalized\"}'::jsonb, updated_at=now()
                 WHERE key=$1 AND value->>'state'='confirmed_not_normalized'",
            ).bind(&mapping.state_key).execute(&mut **txn).await?;
            changed = true;
            "projected"
        } else if record.operation == SourceOperation::Delete {
            "deleted"
        } else {
            "quarantined"
        };
        write_state(
            txn,
            &key,
            &json!({
                "revision": raw.revision, "generation": generation, "time_semantics": semantics,
                "status": status, "observation_id": observation.map(|id| id.to_string()),
            }),
        )
        .await?;
    }
    Ok(changed)
}

struct Observation<'a> {
    instance: &'a str,
    mapping: &'a ConfirmedMapping,
    record: &'a SourceRecord,
    raw: &'a RawRevision,
    fetched: SourceTimestamp,
    at: OffsetDateTime,
    generation: i64,
    value: f64,
}

async fn insert_observation(
    txn: &mut Transaction<'_, Postgres>,
    observation: Observation<'_>,
) -> Result<Uuid, SourcePageError> {
    let Observation {
        instance,
        mapping,
        record,
        raw,
        fetched,
        at,
        generation,
        value,
    } = observation;
    let id = Uuid::new_v4();
    let sequence = i32::try_from(generation).map_err(|_| SourcePageError::Mapping)?;
    sqlx::query(
        "INSERT INTO metric_observations (
           id,metric,value,unit,metric_definition_version_id,observed_at,effective_start,
           recorded_at,uploaded_at,source_calendar_date,source_ref,fetched_at,ingested_at,
           timezone,account,quality,valid,sequence,feature_version
         ) VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,timezone($9,$6)::date,$10,$8,$11,
           $9,$12,'observed',true,$13,'source-plugin-v1')",
    )
    .bind(id)
    .bind(&mapping.metric_name)
    .bind(value)
    .bind(&mapping.draft.unit)
    .bind(mapping.metric_version)
    .bind(OffsetDateTime::from(record.effective_at.instant()))
    .bind(OffsetDateTime::from(record.observed_at.instant()))
    .bind(OffsetDateTime::from(fetched.instant()))
    .bind(&record.source_timezone)
    .bind(raw.id)
    .bind(at)
    .bind(format!("plugin:{}", sha256_hex(instance.as_bytes())))
    .bind(sequence)
    .execute(&mut **txn)
    .await?;
    Ok(id)
}
