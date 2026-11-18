use serde_json::json;
use sqlx::{Postgres, Transaction};

use crate::{
    scenario_packs::pack_keys, system_definitions::initialize_system_events,
    system_metrics::ensure_system_metrics_in,
};

use super::SchemaMigrationError;

const METRIC_REGISTRY_DIGEST: &str =
    "0ee411ffac06caf2a03c69cf14403a23d2e9bd8c953e714849e56a8a364ac709";

pub(super) async fn initialize(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), SchemaMigrationError> {
    let erased: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key='maintenance:erased')")
            .fetch_one(&mut **txn)
            .await?;
    if erased {
        return Ok(());
    }
    initialize_system_events(txn)
        .await
        .map_err(|_| SchemaMigrationError::Registry)?;
    initialize_metrics(txn).await?;
    initialize_packs(txn).await
}

async fn initialize_metrics(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), SchemaMigrationError> {
    let metrics = ensure_system_metrics_in(txn)
        .await
        .map_err(|_| SchemaMigrationError::Registry)?;
    for (key, version) in metrics {
        sqlx::query(
            "UPDATE measurements SET metric_definition_version_id=$2
             WHERE metric=$1 AND metric_definition_version_id IS NULL",
        )
        .bind(key)
        .bind(version.id)
        .execute(&mut **txn)
        .await?;
        sqlx::query(
            "UPDATE metric_observations SET metric_definition_version_id=$2
             WHERE metric=$1 AND metric_definition_version_id IS NULL",
        )
        .bind(key)
        .bind(version.id)
        .execute(&mut **txn)
        .await?;
    }
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('registry:metric:catalog_digest',$1::jsonb)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=now()
         WHERE app_state.value IS DISTINCT FROM excluded.value",
    )
    .bind(json!({"hash":METRIC_REGISTRY_DIGEST}).to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn initialize_packs(txn: &mut Transaction<'_, Postgres>) -> Result<(), SchemaMigrationError> {
    let keys: Vec<_> = pack_keys()
        .map_err(|_| SchemaMigrationError::Registry)?
        .collect();
    sqlx::query(
        "WITH footprint AS (
           SELECT EXISTS(SELECT 1 FROM events) OR EXISTS(SELECT 1 FROM activities)
               OR EXISTS(SELECT 1 FROM health_days) OR EXISTS(SELECT 1 FROM measurements)
               OR EXISTS(SELECT 1 FROM metric_observations) OR EXISTS(SELECT 1 FROM source_payloads)
               OR EXISTS(SELECT 1 FROM timeline_intervals)
               OR EXISTS(SELECT 1 FROM app_state
                          WHERE key IN ('preferences:personal-goals','migration:legacy-scenario-packs'))
               AS legacy
         )
         INSERT INTO module_configs (
            id,owner_id,pack_key,tracking_enabled,collection_enabled,reminders_enabled,
            visible,llm_enabled,settings,revision
         )
         SELECT gen_random_uuid(),owner.id,pack.key,
                footprint.legacy OR pack.key='general_diary',
                footprint.legacy OR pack.key='general_diary',
                footprint.legacy OR pack.key='general_diary',
                footprint.legacy OR pack.key='general_diary',
                footprint.legacy,'{}'::jsonb,1
         FROM people owner CROSS JOIN unnest($1::text[]) AS pack(key) CROSS JOIN footprint
         WHERE owner.singleton AND NOT EXISTS(
             SELECT 1 FROM module_configs config WHERE config.owner_id=owner.id
         )",
    ).bind(keys).execute(&mut **txn).await?;
    Ok(())
}
