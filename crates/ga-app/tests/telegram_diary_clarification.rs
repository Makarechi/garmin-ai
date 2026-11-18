use std::{env, error::Error, fs, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    jobs::{
        JobStoreError,
        telegram_inbound::{TelegramDiaryCommitError, TelegramInboundTarget},
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    update_id: i64,
    edits: Option<i64>,
    message_at: String,
    processing_started: String,
    text: String,
    outbox_text: String,
    form: Value,
}

#[tokio::test]
#[ignore = "compares pinned Python clarification behavior on a disposable TimescaleDB"]
async fn stores_clarification_with_its_reply_and_current_inbox_lease() -> Result<(), Box<dyn Error>>
{
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner_id).execute(&pool).await?;
    sqlx::query(
        "INSERT INTO app_state(key,value)
         VALUES('conversation:pending:telegram:secondary','{\"synthetic\":true}')",
    )
    .execute(&pool)
    .await?;
    let binding = TelegramBinding {
        owner_id,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let target = TelegramInboundTarget {
        owner_id,
        instance_id: "primary",
    };
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var(
        "GA_TEST_TELEGRAM_DIARY_CLARIFICATION",
    )?)?)?;
    assert_eq!(cases.len(), 3);
    let mut previous = None;
    for (index, case) in cases.iter().enumerate() {
        let message_at: OffsetDateTime = case.message_at.parse::<StoredInstant>()?.into();
        let started = case.processing_started.parse()?;
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&pool)
            .await?;
        let now = StoredInstant::try_from(now)?;
        let mut message = json!({
            "message_id":case.edits.unwrap_or(case.update_id),
            "from":{"id":42},
            "chat":{"id":42,"type":"private"},
            "date":message_at.unix_timestamp(),
            "text":case.text
        });
        let update = if case.edits.is_some() {
            message["edit_date"] = json!(message_at.unix_timestamp());
            json!({"update_id":case.update_id,"edited_message":message})
        } else {
            json!({"update_id":case.update_id,"message":message})
        };
        let receipt = binding
            .accept_polled_update(&mut runtime, &update, now, false)
            .await?
            .inbound
            .ok_or("Missing authenticated message")?;
        if case.edits.is_some() {
            assert_eq!(receipt.supersedes_id, previous.map(|(message, _)| message));
            assert_eq!(
                Some(receipt.operation_id),
                previous.map(|(_, operation)| operation)
            );
        }
        previous = Some((receipt.message_id, receipt.operation_id));
        let lease = database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .ok_or("Missing diary lease")?;
        if index == 0 {
            sqlx::query("ALTER TABLE outbox_messages ADD CONSTRAINT synthetic_outbox_failure CHECK (false) NOT VALID")
                .execute(&pool).await?;
            assert!(matches!(
                database
                    .apply_telegram_text_clarification(&lease, started, &case.outbox_text, "ru")
                    .await,
                Err(TelegramDiaryCommitError::Outbox(_))
            ));
            let unchanged: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM inbound_messages WHERE id=$1 AND status='pending')
                  AND NOT EXISTS(SELECT 1 FROM app_state WHERE key='conversation:pending')",
            )
            .bind(receipt.message_id)
            .fetch_one(&pool)
            .await?;
            assert!(unchanged);
            sqlx::query("ALTER TABLE outbox_messages DROP CONSTRAINT synthetic_outbox_failure")
                .execute(&pool)
                .await?;
            sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
                .bind(owner_id)
                .execute(&pool)
                .await?;
            assert!(matches!(
                database
                    .apply_telegram_text_clarification(&lease, started, &case.outbox_text, "ru")
                    .await,
                Err(TelegramDiaryCommitError::BindingChanged)
            ));
            sqlx::query("UPDATE channel_bindings SET external_id='42' WHERE owner_id=$1")
                .bind(owner_id)
                .execute(&pool)
                .await?;
        }
        let queued = database
            .apply_telegram_text_clarification(&lease, started, &case.outbox_text, "ru")
            .await?;
        assert!(queued.created);
        assert!(matches!(
            database
                .apply_telegram_text_clarification(&lease, started, &case.outbox_text, "ru")
                .await,
            Err(TelegramDiaryCommitError::Job(JobStoreError::LeaseLost))
        ));
        let (actual_form, status, reply): (String, String, String) = sqlx::query_as(
            "SELECT state.value::text,i.status,o.intent#>>'{blocks,0,text}'
               FROM app_state state CROSS JOIN inbound_messages i
               CROSS JOIN outbox_messages o
              WHERE state.key='conversation:pending' AND i.id=$1 AND o.id=$2
                AND o.inbound_message_id=i.id",
        )
        .bind(receipt.message_id)
        .bind(queued.outbox_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(serde_json::from_str::<Value>(&actual_form)?, case.form);
        assert_eq!(status, "processed");
        assert_eq!(reply, case.outbox_text);
    }
    let distinct_outbox: i64 = sqlx::query_scalar("SELECT count(*) FROM outbox_messages")
        .fetch_one(&pool)
        .await?;
    assert_eq!(distinct_outbox, 3);
    let separate: String = sqlx::query_scalar(
        "SELECT value::text FROM app_state WHERE key='conversation:pending:telegram:secondary'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(
        serde_json::from_str::<Value>(&separate)?,
        json!({"synthetic":true})
    );
    let event_count: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
        .fetch_one(&pool)
        .await?;
    assert_eq!(event_count, 0);
    Ok(())
}
