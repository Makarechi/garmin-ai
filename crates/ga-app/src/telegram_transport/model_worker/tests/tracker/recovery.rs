use ga_db::{
    jobs::telegram_inbound::{TelegramInboundLease, TrackerSafetyStatus},
    runtime_lease::RuntimeLease,
};

use super::*;

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    Ok(StoredInstant::try_from(at)?)
}

async fn accept(
    database: &Database,
    pool: &PgPool,
    runtime: &mut RuntimeLease,
    version: Uuid,
    update: i64,
) -> Result<TelegramInboundLease, Box<dyn Error>> {
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(pool)
        .await?;
    let at = clock(pool).await?;
    let pending = json!({"definition_version_id":version.to_string(),"button":"tracker_form",
        "action":"log","event_ids":[],"question":"Synthetic cached safety prompt",
        "channel_instance_id":"telegram:primary","created_at":at.to_string()});
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
    binding.accept_polled_update(runtime,&json!({"update_id":update,"message":{"message_id":update,
        "date":OffsetDateTime::from(at).unix_timestamp(),"from":{"id":42},"chat":{"id":42,"type":"private"},
        "text":"Synthetic at 2026-11-18 03:00Z navy"}}),at,true).await?;
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let screen = database
        .claim_telegram_local_screen(target, clock(pool).await?, 150)
        .await?
        .ok_or("Expected safety fixture screen")?;
    database
        .apply_local_telegram_text(
            &screen,
            LocalTelegramText::Safety(TelegramSafetyDecision::Clear),
            "en",
        )
        .await?;
    Ok(database
        .claim_telegram_model_text(target, clock(pool).await?, 150)
        .await?
        .ok_or("Expected safety fixture lease")?)
}

pub(super) async fn assert_safety_recovery(
    worker: &ModelDiaryWorker<'_, '_, LegacyTrackerProvider>,
    pool: &PgPool,
    runtime: &mut RuntimeLease,
) -> Result<(), Box<dyn Error>> {
    let database = worker.database;
    let version: Uuid = sqlx::query_scalar(
        "SELECT v.id FROM event_definition_versions v
        JOIN event_definitions d ON d.id=v.definition_id AND d.current_version=v.version
        WHERE d.key='user.synthetic_chat_text'",
    )
    .fetch_one(pool)
    .await?;
    let destination = ShareDestination::new(DestinationKind::Model, "model:gemini:primary")?;
    let rows: Vec<Value> = serde_json::from_slice(include_bytes!(
        "../../../../../../../migration/contracts/tracker-safety-cache.json"
    ))?;
    let before = worker.sender.calls.load(Ordering::SeqCst);
    for (index, row) in rows.iter().enumerate() {
        let lease = accept(
            database,
            pool,
            runtime,
            version,
            74001 + i64::try_from(index)?,
        )
        .await?;
        sqlx::query("UPDATE jobs SET payload=payload||$2::jsonb WHERE id=$1")
            .bind(lease.job_identity().0)
            .bind(json!({"retained":{"synthetic":true},"form_safety":row["initial"]}).to_string())
            .execute(pool)
            .await?;
        let initial: String = sqlx::query_scalar("SELECT payload::text FROM jobs WHERE id=$1")
            .bind(lease.job_identity().0)
            .fetch_one(pool)
            .await?;
        let mut expected: Value = serde_json::from_str(&initial)?;
        expected["form_safety"] = row["payload"]["form_safety"].clone();
        let requirement = database.telegram_tracker_model_requirement(&lease).await?;
        let mut calls = 0;
        for expected_status in row["statuses"]
            .as_array()
            .ok_or("Expected frozen statuses")?
        {
            let cached = database
                .cached_telegram_tracker_safety(&lease, requirement, destination)
                .await?;
            let status = if let Some(status) = cached {
                status
            } else {
                calls += 1;
                let status = TrackerSafetyStatus::from_stored(
                    row["outcome"].as_str().ok_or("Expected frozen outcome")?,
                )
                .ok_or("Expected supported frozen outcome")?;
                database
                    .record_telegram_tracker_safety(&lease, version, status)
                    .await?;
                status
            };
            assert_eq!(json!(status.as_str()), *expected_status);
        }
        assert_eq!(json!(calls), row["calls"]);
        let stored: String = sqlx::query_scalar("SELECT payload::text FROM jobs WHERE id=$1")
            .bind(lease.job_identity().0)
            .fetch_one(pool)
            .await?;
        assert_eq!(serde_json::from_str::<Value>(&stored)?, expected);
        database
            .finish_telegram_model_notice(&lease, "Synthetic metadata comparison completed")
            .await?;
    }
    assert_eq!(worker.sender.calls.load(Ordering::SeqCst), before);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(pool)
        .await?;
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    for (index, status) in [
        TrackerSafetyStatus::Checked,
        TrackerSafetyStatus::Urgent,
        TrackerSafetyStatus::Unavailable,
    ]
    .into_iter()
    .enumerate()
    {
        let abandoned = accept(
            database,
            pool,
            runtime,
            version,
            75001 + i64::try_from(index)?,
        )
        .await?;
        database
            .record_telegram_tracker_safety(&abandoned, version, status)
            .await?;
        sqlx::query(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",
        )
        .bind(abandoned.job_identity().0)
        .execute(pool)
        .await?;
        assert!(
            database
                .record_telegram_tracker_safety(
                    &abandoned,
                    version,
                    TrackerSafetyStatus::Unavailable
                )
                .await
                .is_err()
        );
        let recovered = database
            .claim_telegram_model_text(target, clock(pool).await?, 150)
            .await?
            .ok_or("Expected cached-safety recovery")?;
        let calls = worker.sender.calls.load(Ordering::SeqCst);
        worker.process(&abandoned, clock(pool).await?).await;
        assert_eq!(worker.sender.calls.load(Ordering::SeqCst), calls);
        let before_events: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
            .fetch_one(pool)
            .await?;
        worker.process(&recovered, clock(pool).await?).await;
        let expected_calls = usize::from(status != TrackerSafetyStatus::Urgent);
        assert_eq!(
            worker.sender.calls.load(Ordering::SeqCst),
            calls + expected_calls
        );
        let completed:(String,String,String)=sqlx::query_as("SELECT i.status,j.status,j.payload->>'form_safety'
            FROM inbound_messages i JOIN jobs j ON j.dedup_key=concat('telegram:inbound:native:',i.id) WHERE i.id=$1")
            .bind(recovered.message_id()).fetch_one(pool).await?;
        assert_eq!(
            completed,
            ("processed".into(), "done".into(), status.as_str().into())
        );
        let after_events: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
            .fetch_one(pool)
            .await?;
        assert_eq!(after_events, before_events + i64::try_from(expected_calls)?);
        let reply: String = sqlx::query_scalar(
            "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE inbound_message_id=$1",
        )
        .bind(recovered.message_id())
        .fetch_one(pool)
        .await?;
        if status == TrackerSafetyStatus::Urgent {
            assert!(reply.contains("112"));
        }
        worker.process(&recovered, clock(pool).await?).await;
        assert_eq!(
            worker.sender.calls.load(Ordering::SeqCst),
            calls + expected_calls
        );
    }
    let (sensitive, definition): (Uuid, Uuid) = sqlx::query_as(
        "SELECT v.id,d.id FROM event_definition_versions v
        JOIN event_definitions d ON d.id=v.definition_id AND d.current_version=v.version
        WHERE d.key='user.synthetic_chat_sensitive'",
    )
    .fetch_one(pool)
    .await?;
    let scopes = [ga_domain::access::Scope::ManageIntegrations]
        .into_iter()
        .collect();
    database
        .grant_tracker_share(
            scopes,
            ga_db::share_policy::TrackerShareGrant {
                definition_id: definition,
                destination,
                categories: &[
                    ga_db::share_policy::ShareCategory::Schema,
                    ga_db::share_policy::ShareCategory::Facts,
                    ga_db::share_policy::ShareCategory::OriginalText,
                ],
                granted_at: clock(pool).await?,
            },
        )
        .await?;
    let denied = accept(database, pool, runtime, sensitive, 76001).await?;
    database
        .record_telegram_tracker_safety(&denied, sensitive, TrackerSafetyStatus::Checked)
        .await?;
    assert!(
        database
            .revoke_tracker_share(scopes, definition, destination)
            .await?
    );
    let calls = worker.sender.calls.load(Ordering::SeqCst);
    let before_events: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
        .fetch_one(pool)
        .await?;
    worker.process(&denied, clock(pool).await?).await;
    assert_eq!(worker.sender.calls.load(Ordering::SeqCst), calls);
    let after_events: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
        .fetch_one(pool)
        .await?;
    assert_eq!(after_events, before_events);
    let stored: String = sqlx::query_scalar("SELECT payload->>'form_safety' FROM jobs WHERE id=$1")
        .bind(denied.job_identity().0)
        .fetch_one(pool)
        .await?;
    assert_eq!(stored, "checked");
    let status: String = sqlx::query_scalar("SELECT status FROM inbound_messages WHERE id=$1")
        .bind(denied.message_id())
        .fetch_one(pool)
        .await?;
    assert_eq!(status, "processed");
    for (index, status) in [
        TrackerSafetyStatus::Checked,
        TrackerSafetyStatus::Urgent,
        TrackerSafetyStatus::Unavailable,
    ]
    .into_iter()
    .enumerate()
    {
        let lease = accept(
            database,
            pool,
            runtime,
            version,
            77001 + i64::try_from(index)?,
        )
        .await?;
        worker.sender.safety_outcome.store(index, Ordering::SeqCst);
        let calls = worker.sender.calls.load(Ordering::SeqCst);
        worker.process(&lease, clock(pool).await?).await;
        let expected_calls = if status == TrackerSafetyStatus::Urgent {
            1
        } else {
            2
        };
        assert_eq!(
            worker.sender.calls.load(Ordering::SeqCst),
            calls + expected_calls
        );
        let stored: String =
            sqlx::query_scalar("SELECT payload->>'form_safety' FROM jobs WHERE id=$1")
                .bind(lease.job_identity().0)
                .fetch_one(pool)
                .await?;
        assert_eq!(stored, status.as_str());
        let completed: String =
            sqlx::query_scalar("SELECT status FROM inbound_messages WHERE id=$1")
                .bind(lease.message_id())
                .fetch_one(pool)
                .await?;
        assert_eq!(completed, "processed");
    }
    worker.sender.safety_outcome.store(0, Ordering::SeqCst);
    println!(
        "Matched 24 frozen cache contracts over 72 reads and recovered all three persisted statuses without repeating the safety SDK call"
    );
    Ok(())
}
