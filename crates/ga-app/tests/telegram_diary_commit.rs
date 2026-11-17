use std::{env, error::Error, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    diary::StoredSystemEvent,
    jobs::{
        JobStoreError,
        telegram_inbound::{TelegramDiaryCommitError, TelegramInboundTarget},
    },
};
use ga_domain::{
    agent_interpretation::{AgentInterpretation, TimeClarification},
    channel::{InboundEnvelope, OutboundIntent},
    metric_contract::UserMetricContract,
    timestamp::StoredInstant,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

const OWNER_TEXT: &str = "Synthetic authenticated migraine, note and wellbeing report 🧪";

#[tokio::test]
#[ignore = "exercises one Telegram diary transaction against disposable TimescaleDB"]
async fn owner_event_reply_and_inbox_commit_together() -> Result<(), Box<dyn Error>> {
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
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let contract = UserMetricContract::from_json(&json!({
        "key":"user.synthetic_energy",
        "labels":{"en":"Synthetic energy"},
        "value_kind":"ordinal",
        "unit":"score_1-10",
        "dimension":"ordinal",
        "scale_id":"user.synthetic_energy",
        "scale_version":1,
        "aggregation":"latest",
        "allowed_methods":["latest","median"],
        "coverage":{"kind":"sparse"},
        "time_semantics":"point",
        "minimum":1,
        "maximum":10,
    }))?;
    let metric = database
        .register_user_metric(owner, true, &contract)
        .await?;
    let wellbeing = database
        .ensure_system_definition("wellbeing_observation")
        .await?;
    sqlx::query(
        "INSERT INTO event_metric_mappings(id,event_definition_version_id,field_id,
                 metric_definition_version_id,projection_version)
         VALUES(gen_random_uuid(),$1,'system.wellbeing_observation.energy',$2,1)",
    )
    .bind(wellbeing.id)
    .bind(metric.id)
    .execute(&pool)
    .await?;
    let binding = TelegramBinding {
        owner_id: owner,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let sent_at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(sent_at)?;
    let message = json!({
        "message_id": 781, "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
        "date": sent_at.unix_timestamp(), "text": OWNER_TEXT,
    });
    let receipt = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":301,"message":message}),
            now,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing synthetic owner message")?;
    let lease = database
        .claim_telegram_inbound(target, now, 60)
        .await?
        .ok_or("Missing synthetic claim")?;
    let report_at = StoredInstant::try_from(sent_at - Duration::minutes(20))?.to_string();
    let source_document = json!({
        "intent": "log", "confidence": 0.95,
        "events": [
            {
                "start": report_at, "timezone": "UTC", "original_text":"model text",
                "payload":{"type":"note","description":"synthetic note"}
            },
            {
                "start": report_at, "timezone": "UTC", "original_text":"model text",
                "payload":{"type":"migraine","severity":5}
            },
            {
                "start": report_at, "timezone": "UTC", "original_text":"model text",
                "payload":{"type":"wellbeing_observation","energy":6}
            }
        ],
    });
    let command = AgentInterpretation::from_value(&source_document)?;
    let build = |input: &InboundEnvelope, saved: &[StoredSystemEvent]| {
        assert_eq!(input.text(), Some(OWNER_TEXT));
        assert_eq!(saved.len(), 3);
        assert_eq!(
            saved[0].snapshot["payload"]["description"],
            "synthetic note"
        );
        OutboundIntent::normalize(json!({
            "owner_id": input.owner_id().to_string(),
            "conversation_id": input.conversation_id().to_string(),
            "channel_instance": {"channel": "telegram", "instance_id":"primary"},
            "blocks": [{"text":"Synthetic confirmation of three saved diary records"}],
        }))
        .ok()
    };
    sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    assert!(matches!(
        database
            .apply_telegram_text_log(&lease, &command, build)
            .await,
        Err(TelegramDiaryCommitError::BindingChanged)
    ));
    sqlx::query("UPDATE channel_bindings SET external_id='42' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    let mut bad_clock = source_document.clone();
    bad_clock["events"][0]["timezone"] = json!("Asia/Tokyo");
    assert!(matches!(
        database
            .apply_telegram_text_log(&lease, &AgentInterpretation::from_value(&bad_clock)?, build)
            .await,
        Err(TelegramDiaryCommitError::TimeClarification(
            TimeClarification::TimezoneMismatch
        ))
    ));
    let mut unevidenced_intake = source_document.clone();
    unevidenced_intake["events"][0]["payload"] = json!({
        "type":"medication","name":"synthetic intake","dose":1,"unit":"mg"
    });
    assert!(matches!(
        database
            .apply_telegram_text_log(
                &lease,
                &AgentInterpretation::from_value(&unevidenced_intake)?,
                build,
            )
            .await,
        Err(TelegramDiaryCommitError::MedicationEvidenceRequired)
    ));
    let mut unsafe_target = source_document.clone();
    unsafe_target["target_event_id"] = json!(Uuid::from_u128(999).to_string());
    assert!(matches!(
        database
            .apply_telegram_text_log(
                &lease,
                &AgentInterpretation::from_value(&unsafe_target)?,
                build
            )
            .await,
        Err(TelegramDiaryCommitError::ExistingContextRequired)
    ));
    sqlx::query("ALTER TABLE outbox_messages ADD CONSTRAINT synthetic_outbox_failure CHECK (false) NOT VALID")
        .execute(&pool).await?;
    assert!(matches!(
        database
            .apply_telegram_text_log(&lease, &command, build)
            .await,
        Err(TelegramDiaryCommitError::Outbox(_))
    ));
    assert_eq!(counts(&pool).await?, (0, 0, 0, 0));
    let pending: String = sqlx::query_scalar("SELECT status FROM inbound_messages WHERE id=$1")
        .bind(receipt.message_id)
        .fetch_one(&pool)
        .await?;
    assert_eq!(pending, "pending");
    sqlx::query("ALTER TABLE outbox_messages DROP CONSTRAINT synthetic_outbox_failure")
        .execute(&pool)
        .await?;
    let queued = database
        .apply_telegram_text_log(&lease, &command, build)
        .await?;
    assert!(queued.created);
    assert!(matches!(
        database
            .apply_telegram_text_log(&lease, &command, build)
            .await,
        Err(TelegramDiaryCommitError::Job(JobStoreError::LeaseLost))
    ));
    let outcome: Value = serde_json::from_str(
        &sqlx::query_scalar::<_, String>(
            "SELECT jsonb_build_object(
             'job',j.status,'inbox',i.status,'outbox',o.state,'operation',o.operation_id,
             'text',(SELECT jsonb_agg(original_text) FROM events),
             'source',(SELECT jsonb_agg(source) FROM events)
         )::text
         FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id)
         JOIN outbox_messages o ON o.inbound_message_id=i.id WHERE o.id=$1",
        )
        .bind(queued.outbox_id)
        .fetch_one(&pool)
        .await?,
    )?;
    assert_eq!(
        outcome,
        json!({
            "job":"done","inbox":"processed","outbox":"queued","operation":receipt.operation_id.to_string(),
            "text":[OWNER_TEXT,OWNER_TEXT,OWNER_TEXT],"source":["telegram_text","telegram_text","telegram_text"],
        })
    );
    let stored = counts(&pool).await?;
    assert_eq!((stored.0, stored.1, stored.2), (3, 3, 1));
    assert_eq!(stored.3, 1);
    let mut edited_message = message;
    edited_message["text"] = json!("Synthetic edit after save");
    let edit = binding
        .accept_polled_update(
            &mut runtime,
            &json!({"update_id":302,"edited_message":edited_message}),
            now,
            false,
        )
        .await?
        .inbound
        .ok_or("Missing edit")?;
    let edit_work = database
        .claim_telegram_inbound(target, now, 60)
        .await?
        .ok_or("Missing edit claim")?;
    assert_eq!(edit_work.message_id(), edit.message_id);
    assert!(matches!(
        database
            .apply_telegram_text_log(&edit_work, &command, build)
            .await,
        Err(TelegramDiaryCommitError::ExistingContextRequired)
    ));
    assert_eq!(counts(&pool).await?, stored);
    Ok(())
}

async fn counts(pool: &PgPool) -> Result<(i64, i64, i64, i64), sqlx::Error> {
    sqlx::query_as(
        "SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log),
                (SELECT count(*) FROM outbox_messages),(SELECT count(*) FROM metric_observations)",
    )
    .fetch_one(pool)
    .await
}
