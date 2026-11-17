use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    telegram_cursor::{
        TelegramCursorError, advance_telegram_poll_cursor, record_telegram_ordering,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;

#[derive(Deserialize)]
struct Case {
    kind: String,
    instance: String,
    now: String,
    arg: Value,
    outcome: Option<i64>,
    state: Value,
}

#[tokio::test]
#[ignore = "compares native bot cursors with pinned Python on disposable TimescaleDB"]
async fn keeps_per_bot_offsets_and_rolls_old_orders_forward() -> Result<(), Box<dyn Error>> {
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
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, current);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_CURSOR")?)?)?;
    let count = cases.len();
    for (index, case) in cases.into_iter().enumerate() {
        let now: StoredInstant = case.now.parse()?;
        let actual = match case.kind.as_str() {
            "seed" => {
                upsert(&pool, &case.instance, &case.arg).await?;
                None
            }
            "read" | "poll_foreign" => {
                let cursor = database.telegram_poll_cursor(&case.instance, now).await?;
                if case.kind == "poll_foreign" {
                    let mut txn = pool.begin().await?;
                    advance_telegram_poll_cursor(
                        &mut txn,
                        &case.instance,
                        case.arg.as_i64().ok_or("Missing synthetic ID")?,
                        now,
                    )
                    .await?;
                    txn.commit().await?;
                }
                cursor.offset
            }
            "order" | "legacy_order" => {
                if case.kind == "legacy_order" {
                    let at: StoredInstant = case.arg["received_at"]
                        .as_str()
                        .ok_or("Missing legacy receipt time")?
                        .parse()?;
                    sqlx::query("DELETE FROM app_state WHERE key='telegram:ordering'")
                        .execute(&pool)
                        .await?;
                    sqlx::query(
                        "INSERT INTO telegram_updates(id,payload,received_at,status)
                                 VALUES(301,'{\"update_id\":301}',$1,'processed')",
                    )
                    .bind(OffsetDateTime::from(at))
                    .execute(&pool)
                    .await?;
                }
                let mut txn = pool.begin().await?;
                let epoch = record_telegram_ordering(&mut txn, &case.instance, now).await?;
                txn.commit().await?;
                Some(epoch)
            }
            _ => return Err("Unrecognized cursor fixture".into()),
        };
        assert_eq!(
            actual, case.outcome,
            "{index} {} {}",
            case.kind, case.instance
        );
        assert_eq!(
            snapshot(&pool).await?,
            case.state,
            "{index} {} {}",
            case.kind,
            case.instance
        );
    }

    let now = "2026-12-10T10:00:00Z".parse()?;
    let previous = snapshot(&pool).await?;
    let mut txn = pool.begin().await?;
    let _ = record_telegram_ordering(&mut txn, "secondary", now).await?;
    advance_telegram_poll_cursor(&mut txn, "primary", 2000, now).await?;
    txn.rollback().await?;
    assert_eq!(snapshot(&pool).await?, previous);

    let mut python = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104623)")
        .execute(&mut *python)
        .await?;
    assert!(matches!(
        database.telegram_poll_cursor("primary", now).await,
        Err(TelegramCursorError::Busy)
    ));
    python.rollback().await?;
    upsert(
        &pool,
        "telegram:offset",
        &json!({"offset":-1,"received_at":"2026-12-10T09:00:00Z"}),
    )
    .await?;
    assert!(matches!(
        database.telegram_poll_cursor("primary", now).await,
        Err(TelegramCursorError::InvalidState)
    ));
    println!("{count} paired Telegram cursor and ordering snapshots matched");
    Ok(())
}

async fn upsert(pool: &PgPool, key: &str, value: &Value) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
         ON CONFLICT (key) DO UPDATE SET value=excluded.value",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(pool)
    .await?;
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let raw: String = sqlx::query_scalar(
        "SELECT coalesce(jsonb_object_agg(key,value),'{}')::text FROM app_state
         WHERE starts_with(key,'telegram:offset') OR starts_with(key,'telegram:ordering')",
    )
    .fetch_one(pool)
    .await?;
    Ok(serde_json::from_str(&raw)?)
}
