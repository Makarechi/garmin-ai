use super::*;
use ga_db::{jobs::telegram_inbound::TelegramTrackerTextPreparation, runtime_lease::RuntimeLease};
use ga_domain::tracker_nl::TrackerExtraction;

pub(super) async fn assert_atomic_commit(
    database: &Database,
    pool: &PgPool,
    runtime: &mut RuntimeLease,
    locale: &str,
) -> Result<(), Box<dyn Error>> {
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(pool)
        .await?;
    let version: Uuid = sqlx::query_scalar("SELECT v.id FROM event_definition_versions v
        JOIN event_definitions d ON d.id=v.definition_id AND d.current_version=v.version WHERE d.key='user.synthetic_chat_text'")
        .fetch_one(pool).await?;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let text = "Synthetic at 2026-11-18 03:00Z navy";
    let wire = json!({"update_id":72001,"message":{"message_id":72001,"date":at.unix_timestamp(),
        "from":{"id":42},"chat":{"id":42,"type":"private"},"text":text}});
    let pending = json!({"definition_version_id":version.to_string(),"button":"tracker_form",
        "action":"log","event_ids":[],"question":"Synthetic atomic tracker prompt",
        "channel_instance_id":"telegram:primary","created_at":now.to_string()});
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('conversation:pending',$1::jsonb)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(pending.to_string())
    .execute(pool)
    .await?;
    let binding = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let receipt = binding
        .accept_polled_update(runtime, &wire, now, true)
        .await?
        .inbound
        .ok_or("Expected atomic receipt")?;
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let screen = database
        .claim_telegram_local_screen(target, now, 150)
        .await?
        .ok_or("Expected atomic screen")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            locale,
        )
        .await?;
    let lease = database
        .claim_telegram_model_text(target, now, 150)
        .await?
        .ok_or("Expected atomic model lease")?;
    let destination = ShareDestination::new(DestinationKind::Model, "model:gemini:primary")?;
    let TelegramTrackerTextPreparation::Model(session) = database
        .prepare_telegram_tracker_text(&lease, "UTC", locale, destination, false)
        .await?
    else {
        return Err("Expected atomic model session".into());
    };
    let raw = json!({"schema_version":"tracker.nl.v1","intent":"create_entry","definition_version_id":version.to_string(),
        "start":"2026-11-18T03:00:00+00:00", "confidence":0.99,
        "start_evidence":{"start":0,"end":text.chars().count(),"quote":text},
        "fields":[{"field_id":"user.synthetic_chat_text.value","value":"navy",
            "evidence":{"start":text.chars().count()-4,"end":text.chars().count(),"quote":"navy"}}]});
    let request = ga_domain::tracker_nl::TrackerTextRequest::from_value(&json!({"text":text,
        "operation_id":"telegram:72001","selected_definition_version_id":version.to_string()}))?;
    let operation = request.operation_key("telegram:42");
    let context = operation.replacen("nl-operation:", "nl-context:", 1);
    let before: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(pool)
            .await?;
    sqlx::query(
        "CREATE FUNCTION synthetic_tracker_outbox_failure() RETURNS trigger LANGUAGE plpgsql
        AS 'BEGIN RAISE EXCEPTION ''synthetic tracker outbox failure''; END;'",
    )
    .execute(pool)
    .await?;
    sqlx::query(
        "CREATE TRIGGER synthetic_tracker_outbox_failure BEFORE INSERT ON outbox_messages
        FOR EACH ROW EXECUTE FUNCTION synthetic_tracker_outbox_failure()",
    )
    .execute(pool)
    .await?;
    let failed = session
        .finish(
            database,
            &lease,
            Some(TrackerExtraction::from_value(&raw)?),
            false,
        )
        .await;
    sqlx::query("DROP TRIGGER synthetic_tracker_outbox_failure ON outbox_messages")
        .execute(pool)
        .await?;
    sqlx::query("DROP FUNCTION synthetic_tracker_outbox_failure()")
        .execute(pool)
        .await?;
    assert!(failed.is_err());
    let after: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(pool)
            .await?;
    assert_eq!(after, before);
    let states: i64 = sqlx::query_scalar("SELECT count(*) FROM app_state WHERE key=ANY($1)")
        .bind(vec![operation.clone(), context.clone()])
        .fetch_one(pool)
        .await?;
    assert_eq!(states, 0);
    let reply_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM outbox_messages WHERE inbound_message_id=$1")
            .bind(receipt.message_id)
            .fetch_one(pool)
            .await?;
    assert_eq!(reply_count, 0);
    let stored: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='conversation:pending'")
            .fetch_one(pool)
            .await?;
    assert_eq!(serde_json::from_str::<Value>(&stored)?, pending);
    let TelegramTrackerTextPreparation::Model(session) = database
        .prepare_telegram_tracker_text(&lease, "UTC", locale, destination, false)
        .await?
    else {
        return Err("Expected atomic retry session".into());
    };
    session
        .finish(
            database,
            &lease,
            Some(TrackerExtraction::from_value(&raw)?),
            false,
        )
        .await?;
    let after: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(pool)
            .await?;
    assert_eq!(after, (before.0 + 1, before.1 + 1));
    let committed: i64 = sqlx::query_scalar("SELECT count(*) FROM app_state WHERE key=ANY($1)")
        .bind(vec![operation, context])
        .fetch_one(pool)
        .await?;
    assert_eq!(committed, 2);
    let completed:(String,String,i64) = sqlx::query_as("SELECT i.status,j.status,
        (SELECT count(*) FROM outbox_messages o WHERE o.inbound_message_id=i.id)
        FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id) WHERE i.id=$1")
        .bind(receipt.message_id).fetch_one(pool).await?;
    assert_eq!(completed, ("processed".into(), "done".into(), 1));
    assert!(
        !binding
            .accept_polled_update(runtime, &wire, now, true)
            .await?
            .inbound
            .ok_or("Expected atomic replay receipt")?
            .created
    );
    assert!(
        database
            .prepare_telegram_tracker_text(&lease, "UTC", locale, destination, false)
            .await
            .is_err()
    );
    println!(
        "An outbox failure rolled back the event, audit, context and receipt. Retrying committed one entry, reply and completed inbox."
    );
    Ok(())
}
