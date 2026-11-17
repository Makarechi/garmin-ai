use std::{collections::HashMap, env, error::Error, fs, str::FromStr};

use ga_db::{
    ReadinessError,
    action_tokens::{
        ActionTokenContext, ActionTokenError, consume_action_token, issue_action_token,
    },
};
use ga_domain::{sha256_hex, timestamp::StoredInstant};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "exchanges synthetic action tokens with pinned Python through disposable TimescaleDB"]
async fn exchanges_tokens_and_preserves_single_use() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let context = ActionTokenContext {
        owner_id: sqlx::query_scalar("SELECT id FROM people")
            .fetch_one(&pool)
            .await?,
        conversation_id: Uuid::from_u128(250),
        revision: 17,
    };
    let tokens: HashMap<String, String> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_ACTION_TOKENS_IN")?)?)?;
    let token = |name: &str| {
        tokens
            .get(name)
            .map(String::as_str)
            .ok_or("Missing action fixture")
    };
    let now = "2099-07-01T05:00:00Z".parse()?;
    let expires_at = "2099-07-01T06:00:00.001000Z".parse()?;
    let confirmed = token("confirm")?;
    let mut tampered = confirmed.to_owned();
    let replacement = if confirmed.starts_with('A') { "B" } else { "A" };
    tampered.replace_range(..1, replacement);
    let mut txn = pool.begin().await?;
    for foreign in [
        ActionTokenContext {
            revision: 18,
            ..context
        },
        ActionTokenContext {
            owner_id: Uuid::from_u128(251),
            ..context
        },
        ActionTokenContext {
            conversation_id: Uuid::from_u128(252),
            ..context
        },
    ] {
        assert_eq!(
            consume_action_token(&mut txn, foreign, confirmed, now).await?,
            None
        );
    }
    assert_eq!(
        consume_action_token(&mut txn, context, &tampered, now).await?,
        None
    );
    assert_eq!(
        consume_action_token(&mut txn, context, "invalid", now).await?,
        None
    );
    assert_eq!(
        consume_action_token(&mut txn, context, token("late")?, expires_at).await?,
        None
    );
    assert_eq!(
        consume_action_token(&mut txn, context, confirmed, now)
            .await?
            .as_deref(),
        Some("confirm")
    );
    txn.commit().await?;

    let mut txn = pool.begin().await?;
    assert_eq!(
        consume_action_token(&mut txn, context, confirmed, now).await?,
        None
    );
    assert_eq!(
        consume_action_token(&mut txn, context, token("rollback")?, now)
            .await?
            .as_deref(),
        Some("rollback")
    );
    sqlx::query("INSERT INTO app_state(key,value) VALUES('synthetic-action-effect','{}')")
        .execute(&mut *txn)
        .await?;
    txn.rollback().await?;
    assert_eq!(
        consume_and_commit(&pool, context, token("rollback")?, now)
            .await?
            .as_deref(),
        Some("rollback")
    );
    let effect: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state WHERE key='synthetic-action-effect')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!effect);
    let raced = token("race")?;
    let (first, second) = tokio::join!(
        consume_and_commit(&pool, context, raced, now),
        consume_and_commit(&pool, context, raced, now),
    );
    let observed = [first?, second?];
    assert_eq!(
        observed
            .iter()
            .flatten()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["race"]
    );

    let mut txn = pool.begin().await?;
    let corrupt = token("corrupt")?;
    let key = format!("action-token:{}", sha256_hex(corrupt.as_bytes()));
    sqlx::query("UPDATE app_state SET value=value-'action_id' WHERE key=$1")
        .bind(&key)
        .execute(&mut *txn)
        .await?;
    assert_eq!(
        consume_action_token(&mut txn, context, corrupt, now).await?,
        None
    );
    txn.rollback().await?;

    let owner_key = format!("action-signing-key:{}", context.owner_id);
    let mut txn = pool.begin().await?;
    sqlx::query("DELETE FROM app_state WHERE key=$1")
        .bind(&owner_key)
        .execute(&mut *txn)
        .await?;
    let rolled_back = issue_action_token(&mut txn, context, "fresh-key", now, expires_at).await?;
    assert_eq!(rolled_back.len(), 57);
    assert_eq!(
        consume_action_token(&mut txn, context, &rolled_back, now)
            .await?
            .as_deref(),
        Some("fresh-key")
    );
    txn.rollback().await?;

    let mut txn = pool.begin().await?;
    assert!(matches!(
        issue_action_token(&mut txn, context, "expired", expires_at, expires_at).await,
        Err(ActionTokenError::InvalidAction)
    ));
    let native = issue_action_token(&mut txn, context, "native", now, expires_at).await?;
    assert_eq!(native.len(), 57);
    txn.commit().await?;
    fs::write(
        env::var("GA_TEST_ACTION_TOKENS_OUT")?,
        json!({"native": native, "rolled_back": rolled_back}).to_string(),
    )?;

    let mut txn = pool.begin().await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('maintenance:erased','{}')")
        .execute(&mut *txn)
        .await?;
    assert!(matches!(
        consume_action_token(&mut txn, context, token("late")?, now).await,
        Err(ActionTokenError::Storage(ReadinessError::StorageDisabled))
    ));
    txn.rollback().await?;

    println!(
        "Pinned Python token format, owner and revision binding, expiry, rollback and concurrent use matched"
    );
    Ok(())
}

async fn consume_and_commit(
    pool: &PgPool,
    context: ActionTokenContext,
    token: &str,
    now: StoredInstant,
) -> Result<Option<String>, Box<dyn Error>> {
    let mut txn = pool.begin().await?;
    sqlx::query("SET LOCAL statement_timeout='2s'")
        .execute(&mut *txn)
        .await?;
    let value = consume_action_token(&mut txn, context, token, now).await?;
    txn.commit().await?;
    Ok(value)
}
