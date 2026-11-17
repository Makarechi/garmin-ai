use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    share_policy::{
        DestinationKind, ShareCategory, ShareDestination, SharePolicyError, TrackerShareGrant,
    },
};
use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

const INITIAL: &str = "00000000-0000-0000-0000-00000000002e";

#[derive(Deserialize)]
struct Case {
    name: String,
    operation: String,
    kind: DestinationKind,
    instance: String,
    categories: Vec<ShareCategory>,
    definition_id: String,
    granted_at: String,
    authorized: bool,
    outcome: String,
    state: Value,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB consent and outbox fixtures"]
async fn changes_consent_and_retires_only_affected_context() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected disposable TimescaleDB URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Database must end in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(&actual, target);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_CONSENT_WRITE_CASES")?)?)?;
    for case in cases {
        let definition_id = case.definition_id.parse()?;
        let granted_at: StoredInstant = case.granted_at.parse()?;
        let destination = ShareDestination::new(case.kind, &case.instance)?;
        let scopes: Scopes = if case.authorized {
            [Scope::ManageIntegrations].into_iter().collect()
        } else {
            [Scope::ManageDefinitions].into_iter().collect()
        };
        let grant = || TrackerShareGrant {
            definition_id,
            destination,
            categories: &case.categories,
            granted_at,
        };
        let saved_revision = match case.name.as_str() {
            "unsupported_revision" => Some(Some(2_i32)),
            "model_schema_removed" => Some(Some(1)),
            "model_original_text_removed" => Some(None),
            _ => None,
        };
        if let Some(revision) = saved_revision {
            let key = format!("tracker-consent:{definition_id}:model:{}", case.instance);
            sqlx::query(
                "UPDATE app_state SET value=CASE WHEN $2::int IS NULL THEN value-'policy_revision'
                   ELSE jsonb_set(value,'{policy_revision}',to_jsonb($2::int)) END WHERE key=$1",
            )
            .bind(key)
            .bind(revision)
            .execute(&inspect)
            .await?;
        }
        if case.name == "channel_facts_removed" {
            let previous = snapshot(&inspect).await?;
            for lock in [72_104_631_i64, 72_104_643] {
                let mut delivery = inspect.begin().await?;
                sqlx::query("SELECT pg_advisory_xact_lock_shared($1)")
                    .bind(lock)
                    .execute(&mut *delivery)
                    .await?;
                assert!(matches!(
                    db.grant_tracker_share(scopes, grant()).await,
                    Err(SharePolicyError::Busy)
                ));
                delivery.rollback().await?;
                assert_eq!(snapshot(&inspect).await?, previous);
            }
            sqlx::raw_sql(
                "CREATE FUNCTION fail_synthetic_consent_write() RETURNS trigger LANGUAGE plpgsql AS $$
                 BEGIN RAISE EXCEPTION 'synthetic consent cleanup failure'; END $$;
                 CREATE TRIGGER fail_synthetic_consent_write BEFORE UPDATE ON app_state
                 FOR EACH ROW WHEN (NEW.key='telegram:reply:secondary-cached')
                 EXECUTE FUNCTION fail_synthetic_consent_write();",
            ).execute(&inspect).await?;
            assert!(matches!(
                db.grant_tracker_share(scopes, grant()).await,
                Err(SharePolicyError::Database)
            ));
            assert_eq!(snapshot(&inspect).await?, previous);
            sqlx::query("DROP TRIGGER fail_synthetic_consent_write ON app_state")
                .execute(&inspect)
                .await?;
        }
        let actual = if case.operation == "grant" {
            db.grant_tracker_share(scopes, grant())
                .await
                .map(|()| "granted")
        } else {
            db.revoke_tracker_share(scopes, definition_id, destination)
                .await
                .map(|removed| if removed { "revoked" } else { "absent" })
        };
        let actual = match actual {
            Ok(outcome) => outcome,
            Err(SharePolicyError::Forbidden) => "PermissionError",
            Err(SharePolicyError::DefinitionMissing) => "LookupError",
            Err(SharePolicyError::InvalidConsent) => "invalid",
            Err(error) => return Err(error.into()),
        };
        assert_eq!(actual, case.outcome, "{}", case.name);
        let actual = snapshot(&inspect).await?;
        for key in ["states", "conversations", "outbox"] {
            assert_eq!(actual[key], case.state[key], "{} at {key}", case.name);
        }
    }
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let stored: String = sqlx::query_scalar(
        "SELECT jsonb_build_object(
             'states', (SELECT jsonb_object_agg(key,value) FROM app_state
                WHERE starts_with(key,'tracker-consent:') OR starts_with(key,'analysis:conversation')
                   OR starts_with(key,'telegram:reply:')),
             'conversations', (SELECT jsonb_object_agg(id::text,jsonb_build_array(state,memory_epoch)) FROM conversations),
             'outbox', (SELECT jsonb_object_agg(dedup_key,jsonb_build_array(state,next_attempt_at IS NOT NULL))
                        FROM outbox_messages)
         )::text",
    )
    .fetch_one(pool)
    .await?;
    let mut document: Value = serde_json::from_str(&stored)?;
    for state in document["states"]
        .as_object_mut()
        .ok_or("Missing state fixture")?
        .values_mut()
    {
        if let Some(epoch) = state.get_mut("epoch") {
            normalize_epoch(epoch);
        }
        if let Some(epochs) = state.get_mut("epochs").and_then(Value::as_object_mut) {
            for epoch in epochs.values_mut() {
                normalize_epoch(epoch);
            }
        }
    }
    for conversation in document["conversations"]
        .as_object_mut()
        .ok_or("Missing conversation fixture")?
        .values_mut()
    {
        normalize_epoch(&mut conversation[1]);
    }
    Ok(document)
}

fn normalize_epoch(value: &mut Value) {
    if let Some(epoch) = value.as_str().filter(|value| value.parse::<Uuid>().is_ok()) {
        *value = json!(if epoch == INITIAL {
            "initial"
        } else {
            "rotated"
        });
    }
}
