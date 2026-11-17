use std::{collections::BTreeMap, env, error::Error, fs, io, str::FromStr};

use ga_db::Database;
use ga_domain::{
    model_gate::{ModelGateIdentity, ModelGateReason, PRIMARY_GEMINI_STATE},
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[derive(Deserialize)]
struct Fixture {
    now: String,
    identities: Vec<Identity>,
    reads: Vec<Read>,
}

#[derive(Deserialize)]
struct Identity {
    instance: String,
    provider: String,
    model: String,
    fallback_enabled: bool,
    fallback_models: Vec<String>,
    consent_models: Vec<String>,
    secrets: BTreeMap<String, Option<String>>,
    expected: (String, i64, String, String),
}

#[derive(Deserialize)]
struct Read {
    name: String,
    identity: usize,
    legacy: Option<Value>,
    scoped: Option<Value>,
    scheduler_paused: bool,
    blocked: Option<(String, String)>,
    scheduler_until: Option<String>,
    model_cooldowns: BTreeMap<String, String>,
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with synthetic provider identities and records"]
async fn reads_pinned_provider_gate_state() -> Result<(), Box<dyn Error>> {
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
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(&actual, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;

    let fixture: Fixture = serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_GATE")?)?)?;
    let now: StoredInstant = fixture.now.parse()?;
    let mut identities = Vec::new();
    for input in fixture.identities {
        let secrets: BTreeMap<_, _> = input
            .secrets
            .iter()
            .map(|(name, value)| (name.as_str(), value.as_deref()))
            .collect();
        let identity = if input.provider == "gemini" {
            ModelGateIdentity::gemini(
                &input.instance,
                &input.model,
                input.fallback_enabled,
                &input
                    .fallback_models
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
                &input
                    .consent_models
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
                secrets["api_key"].ok_or("Missing synthetic Gemini key")?,
            )?
        } else {
            ModelGateIdentity::plugin(&input.instance, &input.provider, &secrets)?
        };
        let (key, lock, configuration, resume) = input.expected;
        assert_eq!(identity.state_key(), key);
        assert_eq!(identity.lock_key(), lock);
        assert_eq!(identity.configuration(), configuration);
        assert_eq!(identity.resume_configuration(), resume);
        identities.push(identity);
    }
    for read in fixture.reads {
        let identity = &identities[read.identity];
        sqlx::query("DELETE FROM app_state WHERE key IN ($1, $2)")
            .bind(PRIMARY_GEMINI_STATE)
            .bind(identity.state_key())
            .execute(&pool)
            .await?;
        for (key, value) in [
            (PRIMARY_GEMINI_STATE, read.legacy),
            (identity.state_key(), read.scoped),
        ] {
            if let Some(value) = value {
                sqlx::query("INSERT INTO app_state(key,value) VALUES ($1,$2::jsonb)")
                    .bind(key)
                    .bind(value.to_string())
                    .execute(&pool)
                    .await?;
            }
        }
        let state = database.read_model_gate(identity, now).await?;
        assert_eq!(
            state.scheduler_paused(),
            read.scheduler_paused,
            "{}",
            read.name
        );
        let blocked = state.blocked.map(|pause| {
            let reason = match pause.reason {
                ModelGateReason::Busy => "busy",
                ModelGateReason::Quota => "quota",
                ModelGateReason::Auth => "auth",
                ModelGateReason::Model => "model",
                ModelGateReason::ModelCooldown => "model_cooldown",
                ModelGateReason::Unavailable => "unavailable",
            };
            (reason.to_owned(), pause.until.to_string())
        });
        assert_eq!(blocked, read.blocked, "{}", read.name);
        assert_eq!(
            state.scheduler_pause_until.map(|at| at.to_string()),
            read.scheduler_until,
            "{}",
            read.name
        );
        let cooldowns: BTreeMap<_, _> = state
            .model_cooldowns
            .into_iter()
            .map(|(model, until)| (model, until.to_string()))
            .collect();
        assert_eq!(cooldowns, read.model_cooldowns, "{}", read.name);
    }
    Ok(())
}
