use std::{collections::BTreeMap, env, error::Error, fs, io, str::FromStr, time::Duration};

use ga_db::{
    Database,
    model_gate::{ModelGateError, ModelGateOutcomeReason, RecordModelGate},
};
use ga_domain::{
    model_gate::{ModelGateIdentity, PRIMARY_GEMINI_INSTANCE, PRIMARY_GEMINI_STATE},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, Connection, PgConnection, PgPool, postgres::PgConnectOptions};

#[derive(Deserialize)]
struct Case {
    name: String,
    instance: String,
    key: String,
    now: String,
    reason: String,
    blocked_until: Option<String>,
    scheduler_until: Option<String>,
    model_cooldowns: BTreeMap<String, String>,
    existing_primary: Option<Value>,
    notify: bool,
    expected: Value,
    expected_notice: Option<String>,
}

fn identity(instance: &str, key: &str) -> Result<ModelGateIdentity, Box<dyn Error>> {
    if instance.starts_with("model:gemini:") {
        Ok(ModelGateIdentity::gemini(
            instance,
            "fixture-model",
            true,
            &["fallback"],
            &["fallback"],
            key,
        )?)
    } else {
        Ok(ModelGateIdentity::plugin(
            instance,
            "openai",
            &BTreeMap::from([("api_key", Some(key))]),
        )?)
    }
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and synthetic outcomes"]
async fn serializes_gate_operations_and_matches_pinned_outcomes() -> Result<(), Box<dyn Error>> {
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
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_GATE_OUTCOMES")?)?)?;
    for case in cases {
        let identity = identity(&case.instance, &case.key)?;
        sqlx::query("DELETE FROM app_state WHERE key IN ($1,$2)")
            .bind(PRIMARY_GEMINI_STATE)
            .bind(identity.state_key())
            .execute(&inspect)
            .await?;
        sqlx::query("DELETE FROM jobs WHERE kind='telegram_provider_notice'")
            .execute(&inspect)
            .await?;
        if let Some(existing) = case.existing_primary {
            sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                .bind(PRIMARY_GEMINI_STATE)
                .bind(existing.to_string())
                .execute(&inspect)
                .await?;
        }
        let mut session = database.lock_model_gate(&identity).await?;
        let reason = match case.reason.as_str() {
            "ready" => ModelGateOutcomeReason::Ready,
            "quota" => ModelGateOutcomeReason::Quota,
            "auth" => ModelGateOutcomeReason::Auth,
            "model" => ModelGateOutcomeReason::Model,
            "model_cooldown" => ModelGateOutcomeReason::ModelCooldown,
            "unavailable" => ModelGateOutcomeReason::Unavailable,
            _ => return Err("Unknown synthetic outcome".into()),
        };
        let now: StoredInstant = case.now.parse()?;
        let deadlines = case
            .model_cooldowns
            .into_iter()
            .map(|(model, at)| Ok((model, at.parse()?)))
            .collect::<Result<BTreeMap<_, _>, ga_domain::timestamp::InvalidStoredInstant>>()?;
        let blocked: Option<StoredInstant> =
            case.blocked_until.as_deref().map(str::parse).transpose()?;
        let scheduler = case
            .scheduler_until
            .as_deref()
            .map(str::parse)
            .transpose()?;
        // Repeated bookkeeping in the same hour must enqueue at most one notice.
        for _ in 0..2 {
            session
                .record(RecordModelGate {
                    now,
                    reason,
                    blocked_until: blocked,
                    scheduler_pause_until: scheduler,
                    model_cooldowns: &deadlines,
                    notify_quota: case.notify,
                })
                .await?;
        }
        let stored: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(identity.state_key())
            .fetch_one(&inspect)
            .await?;
        let stored: Value = serde_json::from_str(&stored)?;
        assert_eq!(stored, case.expected, "{}", case.name);
        assert_eq!(
            session.read(now).await?.blocked.map(|pause| pause.until),
            blocked
        );
        let notices: Vec<String> = sqlx::query_scalar(
            "SELECT dedup_key FROM jobs WHERE kind='telegram_provider_notice' AND status='pending' AND attempts=0"
        ).fetch_all(&inspect).await?;
        assert_eq!(
            notices,
            case.expected_notice.into_iter().collect::<Vec<_>>(),
            "{}",
            case.name
        );
    }

    let primary = identity(PRIMARY_GEMINI_INSTANCE, "synthetic-gemini")?;
    let mut competitor = PgConnection::connect_with(&options).await?;
    assert!(try_lock(&mut competitor).await?);
    assert!(matches!(
        database.lock_model_gate(&primary).await,
        Err(ModelGateError::Busy)
    ));
    sqlx::query("SELECT pg_advisory_unlock_all()")
        .execute(&mut competitor)
        .await?;
    let mut session = database.lock_model_gate(&primary).await?;
    assert!(!try_lock(&mut competitor).await?);
    sqlx::raw_sql(
        "DELETE FROM jobs WHERE kind='telegram_provider_notice';
         CREATE FUNCTION fail_synthetic_model_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN RAISE EXCEPTION 'synthetic bookkeeping failure'; END $$;
         CREATE TRIGGER fail_synthetic_model_outcome BEFORE INSERT OR UPDATE ON app_state
         FOR EACH ROW WHEN (NEW.key='provider:gemini:gate') EXECUTE FUNCTION fail_synthetic_model_outcome();",
    ).execute(&inspect).await?;
    let now: StoredInstant = "2026-03-12T15:00:00+00:00".parse()?;
    let outcome = || RecordModelGate {
        now,
        reason: ModelGateOutcomeReason::Quota,
        blocked_until: None,
        scheduler_pause_until: None,
        model_cooldowns: &EMPTY,
        notify_quota: true,
    };
    assert!(matches!(
        session.record(outcome()).await,
        Err(ModelGateError::Database)
    ));
    let notices: i64 =
        sqlx::query_scalar("SELECT count(*) FROM jobs WHERE kind='telegram_provider_notice'")
            .fetch_one(&inspect)
            .await?;
    assert_eq!(notices, 0);
    drop(session);
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            if try_lock(&mut competitor).await? {
                return Ok::<_, sqlx::Error>(());
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await??;
    sqlx::query("SELECT pg_advisory_unlock_all()")
        .execute(&mut competitor)
        .await?;
    sqlx::query("DROP TRIGGER fail_synthetic_model_outcome ON app_state")
        .execute(&inspect)
        .await?;
    database
        .lock_model_gate(&primary)
        .await?
        .record(outcome())
        .await?;
    let notices: i64 =
        sqlx::query_scalar("SELECT count(*) FROM jobs WHERE kind='telegram_provider_notice'")
            .fetch_one(&inspect)
            .await?;
    assert_eq!(notices, 1);
    Ok(())
}

static EMPTY: BTreeMap<String, StoredInstant> = BTreeMap::new();

async fn try_lock(connection: &mut PgConnection) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
        .bind(72_104_634_i64)
        .fetch_one(connection)
        .await
}
