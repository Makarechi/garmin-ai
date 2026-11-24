use std::{env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{Database, channel_ingress::telegram_poll::TelegramPollError};
use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "compares native and legacy Telegram ownership in disposable TimescaleDB"]
async fn polling_preserves_work_ownership_and_enqueues_atomically() -> Result<(), Box<dyn Error>> {
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
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut lease = database.acquire_runtime_lease().await?;
    let primary = TelegramBinding {
        owner_id: sqlx::query_scalar("SELECT id FROM people")
            .fetch_one(&pool)
            .await?,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let secondary = TelegramBinding {
        instance_id: "secondary",
        ..primary
    };
    let another = TelegramBinding {
        instance_id: "another",
        ..primary
    };
    let now: StoredInstant = "2026-11-17T20:00:00Z".parse()?;
    let item = |id: i64| {
        json!({
            "update_id": id,
            "message": {
                "message_id": id + 100, "from": {"id": 42}, "chat": {"type": "private", "id": 42},
                "date": 1_794_916_800, "text": "synthetic native inbox test",
            }
        })
    };

    // The pinned Python setup stores this update twice, under the raw and the
    // derived storage key, using legacy-v1 with no neutral shadow rows.
    for binding in [primary, secondary] {
        let legacy = binding
            .accept_polled_update(&mut lease, &item(111), now, false)
            .await?;
        assert!(legacy.owned_by_legacy);
        assert!(legacy.inbound.is_none() && !legacy.acknowledgement_queued);
        assert_eq!(
            database
                .telegram_poll_cursor(binding.instance_id, now)
                .await?
                .offset,
            Some(112)
        );
    }
    let new_namespace = another
        .accept_polled_update(&mut lease, &item(111), now, false)
        .await?;
    assert!(!new_namespace.owned_by_legacy);
    assert!(new_namespace.inbound.is_some_and(|receipt| receipt.created));

    let fresh = item(112);
    let created = primary
        .accept_polled_update(&mut lease, &fresh, now, false)
        .await?
        .inbound
        .ok_or("Missing new inbox message")?;
    let repeated = primary
        .accept_polled_update(&mut lease, &fresh, now, false)
        .await?
        .inbound
        .ok_or("Missing inbox replay")?;
    assert!(created.created && !repeated.created);
    assert_eq!(created.message_id, repeated.message_id);

    sqlx::query("ALTER TABLE jobs ADD CONSTRAINT synthetic_job_failure CHECK (kind <> 'telegram_native_inbound') NOT VALID")
        .execute(&pool).await?;
    assert!(matches!(
        primary
            .accept_polled_update(&mut lease, &item(113), now, false)
            .await,
        Err(TelegramPollError::Processing(_))
    ));
    assert_eq!(
        database.telegram_poll_cursor("primary", now).await?.offset,
        Some(113)
    );
    let failed: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM inbound_messages WHERE external_event_id='113')",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!failed);
    sqlx::query("ALTER TABLE jobs DROP CONSTRAINT synthetic_job_failure")
        .execute(&pool)
        .await?;
    assert!(
        primary
            .accept_polled_update(&mut lease, &item(113), now, false)
            .await?
            .inbound
            .is_some_and(|receipt| receipt.created)
    );
    let linked: Vec<(String, String, Uuid)> = sqlx::query_as(
        "SELECT i.channel_instance_id,i.external_event_id,i.id
         FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
         WHERE j.kind='telegram_native_inbound' AND j.payload->>'inbound_message_id'=i.id::text
         ORDER BY i.channel_instance_id,i.external_event_id",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(linked.len(), 3);
    assert_eq!(linked[0].0, "another");
    assert_eq!(linked[1].2, created.message_id);
    assert_eq!(linked[2].1, "113");
    Ok(())
}
