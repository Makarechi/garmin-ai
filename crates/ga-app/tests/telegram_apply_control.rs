use std::{env, error::Error, future::ready, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    channel_ingress::telegram_poll::TelegramPollError,
    delivery::DeliveryState,
    jobs::{
        JobStoreError,
        telegram_inbound::{TelegramControlError, TelegramInboundTarget},
    },
    outbox_claims::{ReplyDestination, ReplyDispatchResult, ReplyObservation, ReplySendOutcome},
    personal_goals::{GoalSelection, PersonalGoal},
    telegram_cursor::TelegramCursorError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

#[path = "support/telegram_ingress.rs"]
mod telegram_ingress;
use telegram_ingress::receive;

#[tokio::test]
#[ignore = "runs authenticated Telegram controls in a disposable TimescaleDB"]
async fn applies_controls_with_their_durable_reply() -> Result<(), Box<dyn Error>> {
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
    let database_name: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(expected, database_name);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let binding = TelegramBinding {
        owner_id: owner,
        instance_id: "primary",
        external_owner_id: 42,
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let clock = OffsetDateTime::now_utc();
    let now = StoredInstant::try_from(clock.replace_nanosecond(clock.microsecond() * 1000)?)?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let wire = |update, message: &str| {
        json!({
            "update_id":update,
            "message":{"message_id":update,"date":clock.unix_timestamp(),
                "from":{"id":42},"chat":{"id":42,"type":"private"},"text":message},
        })
    };
    let mut ingress = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104623)")
        .execute(&mut *ingress)
        .await?;
    assert!(matches!(
        binding
            .accept_polled_update(
                &mut runtime,
                &wire(1, "Synthetic diary message"),
                now,
                false
            )
            .await,
        Err(TelegramPollError::Cursor(TelegramCursorError::Busy))
    ));
    let before_retry: i64 = sqlx::query_scalar("SELECT count(*) FROM inbound_messages")
        .fetch_one(&pool)
        .await?;
    assert_eq!(before_retry, 0);
    ingress.rollback().await?;
    for (update, message) in [
        (1, "Synthetic diary message"),
        (20, "/forget_conversation"),
        (21, "/pause"),
        (22, "/resume"),
        (23, "/today"),
    ] {
        let written = receive(&mut runtime, binding, &wire(update, message), now, false).await?;
        assert!(written.inbound.is_some_and(|row| row.created));
    }
    let diary = database
        .claim_telegram_inbound(target, now, 60)
        .await?
        .ok_or("Missing diary lease")?;

    let (memory,): (Uuid,) = sqlx::query_as(
        "UPDATE conversations SET state='{\"analysis_turns\":[{\"question\":\"synthetic\"}]}'
         WHERE owner_id=$1 AND channel='telegram' RETURNING memory_epoch",
    )
    .bind(owner)
    .fetch_one(&pool)
    .await?;
    sqlx::raw_sql(
        "INSERT INTO app_state(key,value) VALUES
           ('analysis:conversation','{\"turns\":[{\"update_id\":981}]}'),
           ('telegram:reply:981','{\"text\":\"synthetic analysis\",\"kind\":\"analysis\"}');
         CREATE FUNCTION fixture_fail_owner_reply() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN RAISE EXCEPTION 'simulated owner reply failure'; END; $$;
         CREATE TRIGGER fixture_fail_owner_reply BEFORE INSERT ON outbox_messages
         FOR EACH ROW EXECUTE FUNCTION fixture_fail_owner_reply();",
    )
    .execute(&pool)
    .await?;

    let forget = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing forget lease")?;
    assert_ne!(forget.message_id(), diary.message_id());
    assert!(matches!(
        database
            .apply_telegram_owner_control(&forget, 3, "Europe/Bratislava")
            .await,
        Err(TelegramControlError::Outbox(_))
    ));
    let (epoch, count): (Uuid, i64) = sqlx::query_as(
        "SELECT memory_epoch,(SELECT count(*) FROM outbox_messages)
         FROM conversations WHERE owner_id=$1 AND channel='telegram'",
    )
    .bind(owner)
    .fetch_one(&pool)
    .await?;
    assert_eq!((epoch, count), (memory, 0));
    let old_reply: String =
        sqlx::query_scalar("SELECT value->>'text' FROM app_state WHERE key='telegram:reply:981'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(old_reply, "synthetic analysis");
    sqlx::query("DROP TRIGGER fixture_fail_owner_reply ON outbox_messages")
        .execute(&pool)
        .await?;
    let applied = database
        .apply_telegram_owner_control(&forget, 3, "Europe/Bratislava")
        .await?;
    assert!(applied.created);
    assert!(matches!(
        database
            .apply_telegram_owner_control(&forget, 3, "Europe/Bratislava")
            .await,
        Err(TelegramControlError::Job(JobStoreError::LeaseLost))
    ));
    let committed: (Uuid, Option<String>) = sqlx::query_as(
        "SELECT memory_epoch,state->'analysis_turns'->>0 FROM conversations
         WHERE owner_id=$1 AND channel='telegram'",
    )
    .bind(owner)
    .fetch_optional(&pool)
    .await?
    .ok_or("Missing conversation")?;
    assert_ne!(committed.0, memory);
    assert!(committed.1.is_none());
    let retired: String =
        sqlx::query_scalar("SELECT value->>'status' FROM app_state WHERE key='telegram:reply:981'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(retired, "forgotten");
    let duplicate = receive(
        &mut runtime,
        binding,
        &wire(20, "/forget_conversation"),
        now,
        false,
    )
    .await?;
    assert!(duplicate.inbound.is_some_and(|row| !row.created));

    let pause = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing pause lease")?;
    sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    assert!(matches!(
        database
            .apply_telegram_owner_control(&pause, 3, "Europe/Bratislava")
            .await,
        Err(TelegramControlError::Pairing(_) | TelegramControlError::BindingChanged)
    ));
    assert!(
        sqlx::query_scalar::<_, String>(
            "SELECT value::text FROM app_state WHERE key='proactive:enabled'"
        )
        .fetch_optional(&pool)
        .await?
        .is_none()
    );
    sqlx::query("UPDATE channel_bindings SET external_id='42' WHERE owner_id=$1")
        .bind(owner)
        .execute(&pool)
        .await?;
    let mut replay = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock_shared(72104619)")
        .execute(&mut *replay)
        .await?;
    match database
        .apply_telegram_owner_control(&pause, 3, "Europe/Bratislava")
        .await
    {
        Err(
            error @ TelegramControlError::Outbox(ga_db::outbox_enqueue::QueueIntentError::Busy),
        ) => {
            assert!(error.is_busy());
        }
        _ => return Err("Expected retryable outbox lock contention".into()),
    }
    database.defer_telegram_owner_control(&pause, now).await?;
    let unavailable: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT FROM app_state WHERE key='proactive:enabled')
            AND NOT EXISTS(SELECT FROM outbox_messages WHERE inbound_message_id=$1)
            AND EXISTS(SELECT FROM jobs
                       WHERE payload->>'inbound_message_id'=$1::uuid::text
                         AND status='pending' AND attempts=0 AND last_error='TelegramPolicyBusy')",
    )
    .bind(pause.message_id())
    .fetch_one(&pool)
    .await?;
    assert!(unavailable);
    replay.rollback().await?;
    let after_retry = StoredInstant::from_unix_microseconds(now.unix_microseconds() + 300_000)?;
    let pause = database
        .claim_telegram_control(target, after_retry, 60)
        .await?
        .ok_or("Missing rescheduled pause")?;
    assert!(
        database
            .apply_telegram_owner_control(&pause, 3, "Europe/Bratislava")
            .await?
            .created
    );
    let resume = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing resume lease")?;
    assert!(
        database
            .apply_telegram_owner_control(&resume, 4, "Europe/Bratislava")
            .await?
            .created
    );
    let today = database
        .claim_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing status lease")?;
    assert!(
        database
            .apply_telegram_owner_control(&today, 4, "Europe/Bratislava")
            .await?
            .created
    );
    let state: String =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key='proactive:enabled'")
            .fetch_one(&pool)
            .await?;
    let state: serde_json::Value = serde_json::from_str(&state)?;
    assert_eq!(state["enabled"], true);
    assert_eq!(state["update_id"], 22);
    let replies: Vec<(String, String, String, Uuid)> = sqlx::query_as(
        "SELECT i.external_event_id,o.intent#>>'{blocks,0,text}',i.status,o.operation_id
         FROM outbox_messages o JOIN inbound_messages i ON i.id=o.inbound_message_id
         ORDER BY i.external_event_id::bigint",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        replies,
        [
            (
                "20".into(),
                "Контекст аналитического разговора очищен. Записи дневника сохранены.".into(),
                "processed".into(),
                forget.operation_id()
            ),
            (
                "21".into(),
                "Вопросы отключены. Синхронизация продолжается.".into(),
                "processed".into(),
                pause.operation_id()
            ),
            (
                "22".into(),
                "Настройка сохранена: вопросы разрешены. Лимит в день: 4, только вне тихих часов."
                    .into(),
                "processed".into(),
                resume.operation_id()
            ),
            (
                "23".into(),
                "Показатели Garmin ещё не загружены.".into(),
                "processed".into(),
                today.operation_id()
            ),
        ]
    );
    sqlx::raw_sql(
        "UPDATE outbox_messages SET state='delivered';
         INSERT INTO health_days(day,sleep_score,hrv_nightly_avg,resting_hr,body_battery_high,training_readiness_score,sources)
         VALUES ('2026-10-24',10,20,30,40,50,'{}'),('2026-10-25',82.5,NULL,'-0'::double precision,1e-5,60,'{}');",
    ).execute(&pool).await?;
    let summary = "Последние сохранённые показатели за 25.10.2026:\nСон: 82.5\nHRV: нет данных\nПульс покоя: -0.0\nBody Battery максимум: 1e-05\nГотовность: 60.0";
    let notice = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже.";
    for (update, invalidate, pending, expected) in [
        (24, false, false, summary),
        (25, true, false, notice),
        (26, false, true, notice),
    ] {
        if pending {
            sqlx::query(
                "INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,parser_version,status)
                 VALUES(gen_random_uuid(),'garmin_connect','daily_summary','synthetic-day','fixture','synthetic/archive',1,'normalized')",
            ).execute(&pool).await?;
        }
        let accepted = receive(&mut runtime, binding, &wire(update, "/today"), now, false).await?;
        assert!(accepted.inbound.is_some_and(|row| row.created));
        let lease = database
            .claim_telegram_control(target, now, 60)
            .await?
            .ok_or("Missing daily summary")?;
        let recorded = database
            .apply_telegram_owner_control(&lease, 4, "Europe/Bratislava")
            .await?;
        let (captured, guarded): (String, bool) = sqlx::query_as(
            "SELECT intent#>>'{blocks,0,text}',memory_fence ? 'telegram_today' FROM outbox_messages WHERE id=$1",
        ).bind(recorded.outbox_id).fetch_one(&pool).await?;
        assert_eq!(captured, if pending { notice } else { summary });
        assert!(guarded);
        let destination = ReplyDestination {
            owner_id: owner,
            channel: "telegram",
            instance_id: "primary",
        };
        let claim = database
            .claim_outbox_reply(destination, now, 60)
            .await?
            .ok_or("Missing daily reply")?;
        assert_eq!(claim.outbox_id, recorded.outbox_id);
        if invalidate {
            sqlx::raw_sql("INSERT INTO app_state(key,value) VALUES('replay:generation','{\"id\":\"normalization-fixture\"}');
                           UPDATE conversations SET memory_epoch=gen_random_uuid();")
                .execute(&pool).await?;
        }
        let destination = ReplyDestination {
            owner_id: owner,
            channel: "telegram",
            instance_id: "primary",
        };
        let result = database
            .dispatch_telegram_outbox_reply(destination, 42, claim, &[], |intent| {
                assert_eq!(intent["blocks"][0]["text"], expected);
                ready(Some(ReplySendOutcome::Observation(ReplyObservation {
                    state: DeliveryState::ProviderAccepted,
                    observed_at: now,
                    provider_reference: Some(update.to_string()),
                    detail: None,
                })))
            })
            .await?;
        assert!(matches!(
            result,
            ReplyDispatchResult::Recorded {
                state: DeliveryState::ProviderAccepted,
                ..
            }
        ));
        let text: String = sqlx::query_scalar(
            "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE id=$1",
        )
        .bind(recorded.outbox_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(text, expected);
    }
    assert!(
        database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .is_none()
    );
    receive(&mut runtime, binding, &wire(27, "/debug on"), now, false).await?;
    let enable = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing diagnostic enable command")?;
    let enabled = database
        .apply_telegram_owner_control(&enable, 4, "Europe/Bratislava")
        .await?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "primary",
    };
    let old = database
        .claim_outbox_reply(destination(), now, 60)
        .await?
        .ok_or("Missing first diagnostic reply")?;
    assert_eq!(old.outbox_id, enabled.outbox_id);

    receive(&mut runtime, binding, &wire(28, "/debug  off"), now, false).await?;
    let disable = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing diagnostic disable command")?;
    let disabled = database
        .apply_telegram_owner_control(&disable, 4, "Europe/Bratislava")
        .await?;
    assert_eq!(
        database
            .dispatch_telegram_outbox_reply(destination(), 42, old, &[], |_| {
                async { panic!("An outdated diagnostic message was sent") }
            })
            .await?,
        ReplyDispatchResult::Cancelled
    );

    let mut late = wire(29, "/debug on");
    late["message"]["date"] = json!(clock.unix_timestamp() - 1);
    receive(&mut runtime, binding, &late, now, false).await?;
    let late = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing older diagnostic command")?;
    database
        .apply_telegram_owner_control(&late, 4, "Europe/Bratislava")
        .await?;
    let (enabled, update): (bool, i64) = sqlx::query_as(
        "SELECT (value->>'enabled')::bool,(value->>'update_id')::bigint
         FROM app_state WHERE key='telegram:debug'",
    )
    .fetch_one(&pool)
    .await?;
    assert!(!enabled);
    assert_eq!(update, 28);
    let text: String =
        sqlx::query_scalar("SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE id=$1")
            .bind(disabled.outbox_id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(
        text,
        "Диагностика выключена. Включить уведомления об ошибках: /debug on"
    );
    let stored: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM outbox_messages WHERE memory_fence->'telegram_debug'->>1='28'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(stored, 2);
    sqlx::query(
        "UPDATE outbox_messages SET state='delivered' WHERE state='queued' AND memory_fence ? 'telegram_debug'",
    ).execute(&pool).await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','secondary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    receive(&mut runtime, binding, &wire(30, "/debug on"), now, false).await?;
    let primary = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing primary diagnostic command")?;
    let mut replay = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock_shared(72104619)")
        .execute(&mut *replay)
        .await?;
    assert!(matches!(
        database
            .apply_telegram_owner_control(&primary, 4, "Europe/Bratislava")
            .await,
        Err(TelegramControlError::Busy)
    ));
    database.defer_telegram_owner_control(&primary, now).await?;
    replay.rollback().await?;
    let after = StoredInstant::from_unix_microseconds(now.unix_microseconds() + 300_000)?;
    let primary = database
        .claim_local_telegram_control(target, after, 60)
        .await?
        .ok_or("Missing rescheduled diagnostic command")?;
    let primary = database
        .apply_telegram_owner_control(&primary, 4, "Europe/Bratislava")
        .await?;
    let matched = database
        .claim_outbox_reply(destination(), after, 60)
        .await?
        .ok_or("Missing primary diagnostic reply")?;
    assert_eq!(matched.outbox_id, primary.outbox_id);
    let alternate = TelegramBinding {
        instance_id: "secondary",
        ..binding
    };
    receive(
        &mut runtime,
        alternate,
        &wire(30, "/debug off"),
        after,
        false,
    )
    .await?;
    let alternate_target = TelegramInboundTarget {
        instance_id: "secondary",
        ..target
    };
    let next = database
        .claim_local_telegram_control(alternate_target, after, 60)
        .await?
        .ok_or("Missing secondary diagnostic command")?;
    let secondary = database
        .apply_telegram_owner_control(&next, 4, "Europe/Bratislava")
        .await?;
    let truth: (bool, String) = sqlx::query_as(
        "SELECT (value->>'enabled')::bool,value->>'channel_instance_id'
         FROM app_state WHERE key='telegram:debug'",
    )
    .fetch_one(&pool)
    .await?;
    assert_eq!(truth, (false, "telegram:secondary".into()));
    let pair: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT memory_fence->'telegram_debug'->>0,memory_fence->'telegram_debug'->>1,
                memory_fence->'telegram_debug'->>4
         FROM outbox_messages WHERE id IN ($1,$2)",
    )
    .bind(primary.outbox_id)
    .bind(secondary.outbox_id)
    .fetch_all(&pool)
    .await?;
    assert_eq!(pair.len(), 2);
    assert_eq!((&pair[0].0, &pair[0].1), (&pair[1].0, &pair[1].1));
    assert_ne!(pair[0].2, pair[1].2);
    assert_eq!(
        database
            .dispatch_telegram_outbox_reply(destination(), 42, matched, &[], |_| async {
                panic!("An obsolete confirmation from a different bot was sent")
            })
            .await?,
        ReplyDispatchResult::Cancelled
    );

    sqlx::raw_sql(
        r#"UPDATE outbox_messages SET state='delivered' WHERE state='queued' AND memory_fence ? 'telegram_debug';
           DELETE FROM source_payloads WHERE archive_key='synthetic/archive';
           UPDATE health_days SET hrv_nightly_avg=NULL;
           INSERT INTO app_state(key,value) VALUES
             ('integration:garmin','{"status":"reauth_required","reason_class":"AccountMismatch"}'),
             ('freshness:synthetic:live','{"source_key":"live","success_at":"2026-10-25T00:30:00+00:00"}'),
             ('freshness:historical:2000-01-02','{"source_key":"2000-01-02","success_at":"2026-11-17T12:00:00+00:00"}');"#,
    ).execute(&pool).await?;
    // Captured with migration/check_telegram_status.py against pinned Python.
    let expected_status = concat!(
        "Связь с базой работает. Сохранено дней: 2. Обновляемых источников: 1.\n",
        "Garmin: синхронизация остановлена, проверьте привязку владельца аккаунта локально. ",
        "Последний успешный ответ Garmin: 25.10 02:30.\n",
        "Пульс часов: нет сохранённых измерений. Данных недостаточно для оценки текущего состояния. ",
        "Покрытие дня без заполнения пропусков: 0%.\nНочной HRV: нет данных.",
    );
    for (update, stale) in [(31, false), (32, true)] {
        receive(&mut runtime, binding, &wire(update, "/status"), now, false).await?;
        let command = database
            .claim_local_telegram_control(target, now, 60)
            .await?
            .ok_or("Missing status command")?;
        let stored = database
            .apply_telegram_owner_control(&command, 4, "Europe/Bratislava")
            .await?;
        let (captured, guarded): (String, bool) = sqlx::query_as(
            "SELECT intent#>>'{blocks,0,text}',memory_fence ? 'telegram_status' FROM outbox_messages WHERE id=$1",
        ).bind(stored.outbox_id).fetch_one(&pool).await?;
        assert!(guarded);
        assert_eq!(captured, expected_status);
        let lease = database
            .claim_outbox_reply(destination(), now, 60)
            .await?
            .ok_or("Missing status reply")?;
        assert_eq!(lease.outbox_id, stored.outbox_id);
        if stale {
            sqlx::query("UPDATE app_state SET value='{\"id\":\"changed-after-status\"}' WHERE key='replay:generation'")
                .execute(&pool).await?;
        }
        let sent = database
            .dispatch_telegram_outbox_reply(destination(), 42, lease, &[], |intent| {
                assert_eq!(
                    intent["blocks"][0]["text"],
                    if stale { notice } else { expected_status }
                );
                ready(Some(ReplySendOutcome::Observation(ReplyObservation {
                    state: DeliveryState::ProviderAccepted,
                    observed_at: now,
                    provider_reference: Some(update.to_string()),
                    detail: None,
                })))
            })
            .await?;
        assert!(matches!(
            sent,
            ReplyDispatchResult::Recorded {
                state: DeliveryState::ProviderAccepted,
                ..
            }
        ));
    }
    let instructions =
        "\nВыберите нужные: /goals сон самочувствие бег мигрень. Убрать все: /goals нет.";
    let mut old_goals = None;
    for (update, command, description, revision) in [
        (33, "/goals", "Цели пока не выбраны.", 0),
        (34, "/goals СОН, бег", "Ваши цели: бег, сон.", 1),
        (
            35,
            "/goals нет",
            "Личные цели отключены. Спортивная цель выключена.",
            2,
        ),
    ] {
        receive(&mut runtime, binding, &wire(update, command), now, false).await?;
        let lease = database
            .claim_local_telegram_control(target, now, 60)
            .await?
            .ok_or("Missing goals command")?;
        let queued = database
            .apply_telegram_owner_control(&lease, 4, "Europe/Bratislava")
            .await?;
        let (text, fence): (String, i64) = sqlx::query_as(
            "SELECT intent#>>'{blocks,0,text}',(memory_fence->>'telegram_goals')::bigint
             FROM outbox_messages WHERE id=$1",
        )
        .bind(queued.outbox_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(text, format!("{description}{instructions}"));
        assert_eq!(fence, revision);
        let claimed = database
            .claim_outbox_reply(destination(), now, 60)
            .await?
            .ok_or("Missing goals response")?;
        assert_eq!(claimed.outbox_id, queued.outbox_id);
        if let Some(previous) = old_goals.replace(claimed) {
            assert_eq!(
                database
                    .dispatch_telegram_outbox_reply(destination(), 42, previous, &[], |_| async {
                        panic!("A stale goal revision was sent")
                    })
                    .await?,
                ReplyDispatchResult::Cancelled
            );
        }
    }
    let goals = database.personal_goals(owner).await?;
    assert_eq!(goals.revision, 2);
    assert!(goals.configured && goals.goals.is_empty());
    receive(
        &mut runtime,
        binding,
        &wire(36, "/goals сон сон"),
        now,
        false,
    )
    .await?;
    let invalid = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing invalid goals command")?;
    let reply = database
        .apply_telegram_owner_control(&invalid, 4, "Europe/Bratislava")
        .await?;
    let text: String =
        sqlx::query_scalar("SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE id=$1")
            .bind(reply.outbox_id)
            .fetch_one(&pool)
            .await?;
    assert!(text.starts_with("Не удалось выбрать цели."));
    assert_eq!(database.personal_goals(owner).await?, goals);

    let mut voice = wire(37, "/goals сон");
    let voice_fields = voice["message"]
        .as_object_mut()
        .ok_or("Missing fixture message")?;
    let caption = voice_fields
        .remove("text")
        .ok_or("Missing fixture caption")?;
    voice_fields.insert("caption".into(), caption);
    voice_fields.insert(
        "voice".into(),
        json!({"file_id":"synthetic_voice","duration":1}),
    );
    let accepted = receive(&mut runtime, binding, &voice, now, false).await?;
    assert!(accepted.inbound.is_some_and(|receipt| receipt.created));
    let original = wire(38, "Synthetic unsent diary text");
    receive(&mut runtime, binding, &original, now, false).await?;
    let mut edited = original["message"].clone();
    edited["text"] = json!("/goals сон");
    edited["edit_date"] = json!(clock.unix_timestamp() + 1);
    let accepted = receive(
        &mut runtime,
        binding,
        &json!({"update_id":39,"edited_message":edited}),
        now,
        false,
    )
    .await?;
    assert!(accepted.inbound.is_some_and(|receipt| receipt.created));
    receive(&mut runtime, binding, &wire(40, "/goals"), now, false).await?;
    let menu = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("The goals menu was held by a diary message")?;
    database
        .apply_telegram_owner_control(&menu, 4, "Europe/Bratislava")
        .await?;
    assert_eq!(database.personal_goals(owner).await?, goals);

    let api_at = StoredInstant::try_from(OffsetDateTime::from(now) - Duration::seconds(30))?;
    let saved = database
        .select_personal_goals(
            owner,
            GoalSelection {
                expected_revision: goals.revision,
                goals: &[PersonalGoal::Wellbeing],
                now: api_at,
                command: None,
            },
        )
        .await?;
    let mut earlier = wire(41, "/goals бег");
    earlier["message"]["date"] = json!(clock.unix_timestamp() - 60);
    receive(&mut runtime, binding, &earlier, now, false).await?;
    let delayed = database
        .claim_local_telegram_control(target, now, 60)
        .await?
        .ok_or("Missing delayed goal command")?;
    database
        .apply_telegram_owner_control(&delayed, 4, "Europe/Bratislava")
        .await?;
    assert_eq!(database.personal_goals(owner).await?, saved);
    for (update, text) in [(99, "/goals сон"), (100, "/goals нет")] {
        receive(&mut runtime, binding, &wire(update, text), now, false).await?;
    }
    for expected in [vec![PersonalGoal::Sleep], Vec::new()] {
        let next = database
            .claim_local_telegram_control(target, now, 60)
            .await?
            .ok_or("Missing same-second goal command")?;
        database
            .apply_telegram_owner_control(&next, 4, "Europe/Bratislava")
            .await?;
        assert_eq!(database.personal_goals(owner).await?.goals, expected);
    }
    println!(
        "Verified durable owner controls, Garmin status, goals and suppression of stale replies"
    );
    Ok(())
}
