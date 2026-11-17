use std::{env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    jobs::{
        JobStoreError,
        telegram_inbound::{TelegramInboundState, TelegramInboundTarget},
    },
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB database to exercise native Telegram lease recovery"]
async fn claims_preserve_epochs_and_fence_superseded_work() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|db| db.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let binding = TelegramBinding {
        owner_id: sqlx::query_scalar("SELECT id FROM people")
            .fetch_one(&pool)
            .await?,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let target = TelegramInboundTarget {
        owner_id: binding.owner_id,
        instance_id: binding.instance_id,
    };
    let before_gap: StoredInstant = "2026-11-08T20:00:00Z".parse()?;
    let now: StoredInstant = "2026-11-17T20:00:00Z".parse()?;
    let after = |seconds: i64| {
        StoredInstant::from_unix_microseconds(now.unix_microseconds() + seconds * 1_000_000)
    };
    let message = |id: i64, text: &str| {
        json!({
            "message_id": id, "from": {"id": 42}, "chat": {"type": "private", "id": 42},
            "date": 1794916800, "text": text,
        })
    };
    let older = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":900,"message":message(1,"before simulated outage")}),
            before_gap,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing pre-outage delivery")?;
    assert!(
        runtime
            .owned_telegram_poll_cursor("primary", now)
            .await?
            .reset
    );
    let newer = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":10,"message":message(2,"after simulated outage")}),
            now,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing post-outage delivery")?;
    sqlx::query(
        "UPDATE jobs SET run_at=$2 WHERE dedup_key=concat('telegram:inbound:native:',$1::uuid)",
    )
    .bind(older.message_id)
    .bind(time::OffsetDateTime::from(after(4)?))
    .execute(&pool)
    .await?;
    assert!(
        database
            .claim_telegram_inbound(target, now, 2)
            .await?
            .is_none()
    );
    assert!(
        database
            .claim_telegram_inbound(
                TelegramInboundTarget {
                    owner_id: Uuid::from_u128(1),
                    ..target
                },
                after(4)?,
                2
            )
            .await?
            .is_none()
    );
    let (left, right) = tokio::join!(
        database.claim_telegram_inbound(target, after(4)?, 2),
        database.claim_telegram_inbound(target, after(4)?, 2),
    );
    let claims = [left?, right?];
    assert_eq!(claims.iter().flatten().count(), 1);
    let first = claims
        .into_iter()
        .flatten()
        .next()
        .ok_or("Missing first lease")?;
    assert_eq!(first.message_id(), older.message_id);
    assert!(
        database
            .claim_telegram_inbound(target, after(5)?, 2)
            .await?
            .is_none()
    );

    let restarted = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let replacement = restarted
        .claim_telegram_inbound(target, after(7)?, 2)
        .await?
        .ok_or("Missing expired lease replacement")?;
    assert_eq!(replacement.message_id(), older.message_id);
    assert!(matches!(
        database
            .finish_telegram_inbound(&first, after(7)?, TelegramInboundState::Processed)
            .await,
        Err(JobStoreError::LeaseLost)
    ));
    restarted
        .finish_telegram_inbound(&replacement, after(7)?, TelegramInboundState::Processed)
        .await?;
    let initial = restarted
        .claim_telegram_inbound(target, after(7)?, 2)
        .await?
        .ok_or("Missing new epoch lease")?;
    assert_eq!(initial.message_id(), newer.message_id);
    assert_eq!(initial.operation_id(), newer.operation_id);
    let stored: Value = serde_json::from_str(initial.envelope_json())?;
    assert_eq!(stored["text"], "after simulated outage");

    let edit = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":11,"edited_message":message(2,"revised after outage")}),
            after(8)?,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing revised delivery")?;
    assert_eq!(edit.supersedes_id, Some(newer.message_id));
    assert!(
        restarted
            .claim_telegram_inbound(target, after(10)?, 2)
            .await?
            .is_none()
    );
    let latest = restarted
        .claim_telegram_inbound(target, after(10)?, 2)
        .await?
        .ok_or("Missing new revision lease")?;
    assert_eq!(latest.message_id(), edit.message_id);
    assert_eq!(latest.operation_id(), newer.operation_id);
    restarted
        .finish_telegram_inbound(&latest, after(10)?, TelegramInboundState::Invalid)
        .await?;
    assert!(
        restarted
            .claim_telegram_inbound(target, after(11)?, 2)
            .await?
            .is_none()
    );
    let cooldown_item = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":12,"message":message(3,"wait for cooldown")}),
            after(11)?,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing cooldown delivery")?;
    let cooling = restarted
        .claim_telegram_inbound(target, after(11)?, 3)
        .await?
        .ok_or("Missing cooldown claim")?;
    restarted
        .defer_telegram_inbound(&cooling, after(11)?, after(20)?)
        .await?;
    assert!(
        restarted
            .claim_telegram_inbound(target, after(19)?, 2)
            .await?
            .is_none()
    );
    let retried = restarted
        .claim_telegram_inbound(target, after(20)?, 2)
        .await?
        .ok_or("Missing deferred delivery")?;
    assert_eq!(retried.message_id(), cooldown_item.message_id);
    assert!(matches!(
        restarted
            .finish_telegram_inbound(&cooling, after(20)?, TelegramInboundState::Processed)
            .await,
        Err(JobStoreError::LeaseLost)
    ));
    restarted
        .finish_telegram_inbound(&retried, after(20)?, TelegramInboundState::Processed)
        .await?;
    let attempts: i32 = sqlx::query_scalar(
        "SELECT attempts FROM jobs WHERE dedup_key=concat('telegram:inbound:native:',$1::uuid)",
    )
    .bind(cooldown_item.message_id)
    .fetch_one(&pool)
    .await?;
    assert_eq!(attempts, 1);

    let exhausted = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":13,"message":message(4,"retire failed job")}),
            after(21)?,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing exhausted delivery")?;
    sqlx::query(
        "UPDATE jobs SET attempts=8 WHERE dedup_key=concat('telegram:inbound:native:',$1::uuid)",
    )
    .bind(exhausted.message_id)
    .execute(&pool)
    .await?;
    assert!(
        restarted
            .claim_telegram_inbound(target, after(21)?, 2)
            .await?
            .is_none()
    );
    let states: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT i.external_event_id,i.status,j.status FROM inbound_messages i
         JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
         ORDER BY i.external_event_id",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        states,
        [
            ("10".into(), "processed".into(), "done".into()),
            ("11".into(), "invalid".into(), "done".into()),
            ("12".into(), "processed".into(), "done".into()),
            ("13".into(), "invalid".into(), "failed".into()),
            ("900".into(), "processed".into(), "done".into()),
        ]
    );
    Ok(())
}
