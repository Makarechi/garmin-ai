use std::{env, error::Error, fs, future::ready};

use ga_db::{
    Database,
    outbox_claims::ReplyDestination,
    proactive_control::{
        DiaryQuestionKind, DiaryQuestionOutcome, NotificationExclusions, NotificationPolicy,
        ProactiveCommand, ProactiveControlError, QuestionDispatchResult, ReservedDiaryQuestion,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{PgPool, postgres::PgConnectOptions};
use time::{Duration, OffsetDateTime, Time};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    owner: String,
    now: String,
}

#[tokio::test]
#[ignore = "requires migration/check_diary_question_generation.py and its disposable database"]
async fn dispatches_once_and_preserves_ambiguous_delivery() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let suffix = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture database URL")?;
    let options: PgConnectOptions = format!("postgresql://{suffix}").parse()?;
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Expected a disposable test database".into());
    }
    let pool = PgPool::connect_with(options).await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_QUESTIONS")?)?)?;
    let owner: Uuid = fixture.owner.parse()?;
    let now: StoredInstant = fixture.now.parse()?;
    let retry_at: StoredInstant = (OffsetDateTime::from(now) + Duration::minutes(1)).try_into()?;
    let pause = ProactiveCommand::Telegram {
        message_at: retry_at,
        received_at: retry_at,
        ordering_epoch: 0,
        update_id: 412,
    };
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "fixture",
    };
    let policy = NotificationPolicy {
        timezone: "UTC",
        default_enabled: true,
        daily_budget: Some(4),
        quiet_start: Time::MIDNIGHT,
        quiet_end: Time::MIDNIGHT,
        snoozed_until: None,
        destination: None,
        exclusions: NotificationExclusions::default(),
    };
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','fixture','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    sqlx::query("UPDATE app_state SET value='{ \"channel\": {\"channel\":\"telegram\",\"instance_id\":\"fixture\"} }' WHERE key='preferences:onboarding'")
        .execute(&pool).await?;
    database.generate_diary_questions(owner, now, "UTC").await?;
    let closed = database
        .reserve_diary_question(owner, &policy, now)
        .await?
        .ok_or("Missing migraine reservation")?;
    let closed_event = closed.event_id.ok_or("Missing migraine")?;
    sqlx::query("UPDATE events SET \"end\"=$2,topology='bounded_interval' WHERE id=$1")
        .bind(closed_event)
        .bind(OffsetDateTime::from(now))
        .execute(&pool)
        .await?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &closed, &policy, now, |_| never_send())
            .await?,
        QuestionDispatchResult::Cancelled
    );
    assert_eq!(state(&pool, closed.id).await?, ("answered".into(), None));

    let question = database
        .reserve_diary_question(owner, &policy, now)
        .await?
        .ok_or("Missing other migraine")?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 43, &question, &policy, now, |_| never_send())
            .await?,
        QuestionDispatchResult::Unavailable
    );
    assert_eq!(state(&pool, question.id).await?, ("pending".into(), None));
    let question = database
        .reserve_diary_question(owner, &policy, now)
        .await?
        .ok_or("Missing matched reservation")?;
    let deferred = database
        .dispatch_diary_question(destination(), 42, &question, &policy, now, |text| {
            let database = &database;
            let pool = &pool;
            let question = &question;
            async move {
                assert!(!text.is_empty());
                assert_eq!(
                    marker(pool, question.id).await.unwrap_or_default()["status"],
                    "sending"
                );
                let duplicate = database
                    .dispatch_diary_question(destination(), 42, question, &policy, now, |_| {
                        never_send()
                    })
                    .await;
                assert!(matches!(duplicate, Err(ProactiveControlError::Busy)));
                assert!(matches!(
                    database.set_proactive_control(owner, pause, false).await,
                    Err(ProactiveControlError::Busy)
                ));
                DiaryQuestionOutcome::RetryAt(retry_at)
            }
        })
        .await?;
    assert_eq!(deferred, QuestionDispatchResult::Deferred(retry_at));
    assert_eq!(state(&pool, question.id).await?, ("pending".into(), None));
    assert_eq!(
        marker(&pool, question.id).await?["retry_at"],
        retry_at.to_string()
    );
    let retried = database
        .reserve_diary_question(owner, &policy, retry_at)
        .await?
        .ok_or("Missing retry")?;
    assert_eq!(retried.id, question.id);
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &retried, &policy, retry_at, |_| {
                ready(DiaryQuestionOutcome::Accepted(781))
            })
            .await?,
        QuestionDispatchResult::Sent
    );
    assert_eq!(
        marker(&pool, retried.id).await?,
        json!({"status":"sent","message_id":781,"kind":"diary","formatted":true,"channel_instance_id":"telegram:fixture"})
    );

    let caffeine = database
        .reserve_diary_question(owner, &policy, retry_at)
        .await?
        .ok_or("Missing caffeine reservation")?;
    assert_eq!(caffeine.kind, DiaryQuestionKind::Caffeine);
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &caffeine, &policy, retry_at, |_| {
                ready(DiaryQuestionOutcome::Uncertain)
            })
            .await?,
        QuestionDispatchResult::Uncertain
    );
    assert_eq!(state(&pool, caffeine.id).await?.0, "uncertain");
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &caffeine, &policy, retry_at, |_| {
                never_send()
            })
            .await?,
        QuestionDispatchResult::Unavailable
    );

    let interrupted = existing_reservation(&pool, now).await?;
    let unsent = existing_reservation(&pool, now).await?;
    let accepted = existing_reservation(&pool, now).await?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,'{\"status\":\"sent\",\"message_id\":882}')",
    )
    .bind(format!("outbox:question:{}:0", accepted.id))
    .execute(&pool)
    .await?;
    sqlx::raw_sql(
        "CREATE FUNCTION fixture_fail_question_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF NEW.key LIKE 'outbox:question:%' AND NEW.value->>'status' = 'sent'
             THEN RAISE EXCEPTION 'Synthetic receipt write failure'; END IF;
             RETURN NEW;
           END $$;
         CREATE TRIGGER fixture_fail_question_receipt BEFORE UPDATE ON app_state
         FOR EACH ROW EXECUTE FUNCTION fixture_fail_question_receipt();",
    )
    .execute(&pool)
    .await?;
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &interrupted, &policy, now, |_| {
                ready(DiaryQuestionOutcome::Accepted(883))
            })
            .await?,
        QuestionDispatchResult::Uncertain,
    );
    assert_eq!(state(&pool, interrupted.id).await?.0, "sending");
    assert_eq!(marker(&pool, interrupted.id).await?["status"], "sending");
    let mut singleton = database.acquire_runtime_lease().await?;
    assert_eq!(singleton.recover_diary_question_deliveries().await?, 3);
    assert_eq!(state(&pool, interrupted.id).await?.0, "uncertain");
    assert_eq!(state(&pool, accepted.id).await?.0, "sent");
    assert_eq!(state(&pool, unsent.id).await?, ("pending".into(), None));
    assert_eq!(singleton.recover_diary_question_deliveries().await?, 0);
    let pause_preempts = existing_reservation(&pool, now).await?;
    let paused = database.set_proactive_control(owner, pause, false).await?;
    assert!(paused.changed && !paused.enabled);
    assert_eq!(
        database
            .dispatch_diary_question(destination(), 42, &pause_preempts, &policy, now, |_| {
                never_send()
            })
            .await?,
        QuestionDispatchResult::Unavailable
    );
    assert_eq!(state(&pool, pause_preempts.id).await?.0, "cancelled");
    assert_eq!(state(&pool, retried.id).await?.0, "sent");
    println!(
        "Verified current episode and pairing, one dispatch, pause ordering, safe retry and ambiguous restart recovery"
    );
    Ok(())
}

fn never_send() -> std::future::Ready<DiaryQuestionOutcome> {
    panic!("The fenced question must not be sent");
}

async fn state(pool: &PgPool, id: Uuid) -> Result<(String, Option<OffsetDateTime>), sqlx::Error> {
    sqlx::query_as("SELECT status,sent_at FROM pending_questions WHERE id=$1")
        .bind(id)
        .fetch_one(pool)
        .await
}

async fn marker(pool: &PgPool, id: Uuid) -> Result<Value, Box<dyn Error>> {
    let raw: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
        .bind(format!("outbox:question:{id}:0"))
        .fetch_one(pool)
        .await?;
    Ok(serde_json::from_str(&raw)?)
}

async fn existing_reservation(
    pool: &PgPool,
    now: StoredInstant,
) -> Result<ReservedDiaryQuestion, sqlx::Error> {
    let id = Uuid::new_v4();
    let text = "Synthetic booked question".to_owned();
    sqlx::query(
        "INSERT INTO pending_questions(id,kind,text,evidence,priority,dedup_key,
         earliest_send_at,expires_at,sent_at,attempts,status)
         VALUES($1,'caffeine',$2,'{\"day\":\"2026-10-25\",\"timezone\":\"Europe/Bratislava\"}',
            0.6,$1::uuid::text,$3::timestamptz,$3+interval '48 hours',$3,1,'sending')",
    )
    .bind(id)
    .bind(&text)
    .bind(OffsetDateTime::from(now))
    .execute(pool)
    .await?;
    Ok(ReservedDiaryQuestion {
        id,
        kind: DiaryQuestionKind::Caffeine,
        text,
        event_id: None,
        attempt: 1,
        reserved_at: now,
    })
}
