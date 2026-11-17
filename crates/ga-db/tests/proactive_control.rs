use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    proactive_control::{ProactiveCommand, ProactiveControlError},
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    lane: String,
    received_at: String,
    message_at: Option<String>,
    ordering_epoch: Option<i64>,
    update_id: Option<i64>,
    enabled: bool,
    changed: bool,
    state: Value,
}

#[tokio::test]
#[ignore = "compares owner control with pinned Python on disposable TimescaleDB"]
async fn records_the_latest_command_and_cancels_only_unsent_work() -> Result<(), Box<dyn Error>> {
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
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(expected, current);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    seed(&pool, owner).await?;

    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_PROACTIVE_CONTROL")?)?)?;
    let count = cases.len();
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    for (index, case) in cases.into_iter().enumerate() {
        let received_at = case.received_at.parse()?;
        let command = match case.lane.as_str() {
            "telegram" => ProactiveCommand::Telegram {
                received_at,
                message_at: case.message_at.ok_or("Missing message time")?.parse()?,
                ordering_epoch: case.ordering_epoch.ok_or("Missing ordering epoch")?,
                update_id: case.update_id.ok_or("Missing update ID")?,
            },
            "web" => ProactiveCommand::Web { received_at },
            _ => return Err("Unknown reference channel".into()),
        };
        let result = database
            .set_proactive_control(owner, command, case.enabled)
            .await?;
        assert_eq!(result.changed, case.changed, "case {index}");
        assert_eq!(
            result.enabled, case.state["policy"]["enabled"],
            "case {index}"
        );
        assert_eq!(snapshot(&pool).await?, case.state, "case {index}");
    }

    let retry = ProactiveCommand::Web {
        received_at: "2026-09-07T12:01:00Z".parse()?,
    };
    let before = snapshot(&pool).await?;
    let mut sending = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock_shared(72104621)")
        .execute(&mut *sending)
        .await?;
    assert!(matches!(
        database.set_proactive_control(owner, retry, true).await,
        Err(ProactiveControlError::Busy)
    ));
    assert_eq!(snapshot(&pool).await?, before);
    sending.rollback().await?;
    assert!(matches!(
        database
            .set_proactive_control(Uuid::from_u128(1), retry, true)
            .await,
        Err(ProactiveControlError::UnknownOwner)
    ));
    assert_eq!(snapshot(&pool).await?, before);
    assert!(
        database
            .set_proactive_control(owner, retry, true)
            .await?
            .enabled
    );

    let before = snapshot(&pool).await?;
    let pause = ProactiveCommand::Web {
        received_at: "2026-09-07T12:02:00Z".parse()?,
    };
    sqlx::raw_sql(
        "UPDATE pending_questions SET status='pending',sent_at=NULL WHERE dedup_key='fixture:pending';
         UPDATE outbox_messages SET state='queued',next_attempt_at=now() WHERE dedup_key='fixture:initiative';
         CREATE FUNCTION fixture_fail_pause() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN IF NEW.status='cancelled' THEN RAISE EXCEPTION 'simulated failure'; END IF;
         RETURN NEW; END; $$;
         CREATE TRIGGER fixture_fail_pause BEFORE UPDATE ON pending_questions
         FOR EACH ROW EXECUTE FUNCTION fixture_fail_pause();"
    ).execute(&pool).await?;
    let retry_snapshot = snapshot(&pool).await?;
    assert_eq!(retry_snapshot["policy"], before["policy"]);
    assert!(matches!(
        database.set_proactive_control(owner, pause, false).await,
        Err(ProactiveControlError::Database)
    ));
    assert_eq!(snapshot(&pool).await?, retry_snapshot);
    sqlx::query("DROP TRIGGER fixture_fail_pause ON pending_questions")
        .execute(&pool)
        .await?;
    assert!(
        !database
            .set_proactive_control(owner, pause, false)
            .await?
            .enabled
    );
    assert_eq!(
        snapshot(&pool).await?["outbox"]["fixture:initiative"][0],
        "cancelled"
    );
    println!(
        "{count} complete owner-control snapshots matched pinned Python, plus delivery lock and failure recovery"
    );
    Ok(())
}

async fn seed(pool: &PgPool, owner: Uuid) -> Result<(), sqlx::Error> {
    let conversation = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,
            external_conversation_id,memory_epoch,state,share_owner_memory)
         VALUES($1,$2,'web','primary','synthetic-proactive-fixture',$3,'{}',false)",
    )
    .bind(conversation)
    .bind(owner)
    .bind(Uuid::new_v4())
    .execute(pool)
    .await?;
    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,intent,state,
            next_attempt_at,dedup_key,attempts)
         SELECT gen_random_uuid(),$1,$2,gen_random_uuid(),jsonb_build_object('initiative', initiative),
            state,CASE WHEN state='queued' THEN TIMESTAMPTZ '2026-09-07T12:00:00Z' ELSE NULL END,
            'fixture:'||key,0
         FROM (VALUES ('initiative','queued',true),('ordinary','queued',false),
                      ('uncertain','uncertain',true)) AS x(key,state,initiative)",
    ).bind(owner).bind(conversation).execute(pool).await?;
    sqlx::raw_sql(
        "INSERT INTO pending_questions(id,kind,text,evidence,priority,earliest_send_at,expires_at,
            sent_at,status,dedup_key,attempts)
         SELECT gen_random_uuid(),'migraine','synthetic','{}',1,'2026-09-07T12:00:00Z',
            '2026-09-08T12:00:00Z',CASE WHEN key='pending' THEN NULL ELSE TIMESTAMPTZ '2026-09-07T12:00:00Z' END,
            key,'fixture:'||key,0
         FROM (VALUES ('pending'),('sending'),('sent')) AS x(key);
         INSERT INTO insights(id,category,statement,evidence,sample_size,status,dedup_key)
         SELECT gen_random_uuid(),'synthetic','synthetic','{}',5,key,'fixture:'||key
         FROM (VALUES ('accepted'),('candidate')) AS x(key);
         INSERT INTO app_state(key,value) VALUES
           ('insight:last:reservation','{\"reservation\":\"synthetic\"}'),
           ('insight:last:retained','{\"reservation\":false}');",
    ).execute(pool).await?;
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let value: String = sqlx::query_scalar(
        "SELECT jsonb_build_object(
            'policy',(SELECT value FROM app_state WHERE key='proactive:enabled'),
            'outbox',(SELECT jsonb_object_agg(dedup_key,jsonb_build_array(state,next_attempt_at IS NOT NULL))
                      FROM outbox_messages WHERE dedup_key LIKE 'fixture:%'),
            'questions',(SELECT jsonb_object_agg(dedup_key,jsonb_build_array(status,sent_at IS NOT NULL,
                           evidence->'cancel_reason')) FROM pending_questions WHERE dedup_key LIKE 'fixture:%'),
            'insights',(SELECT jsonb_object_agg(dedup_key,jsonb_build_array(status,evidence->'cancel_reason'))
                        FROM insights WHERE dedup_key LIKE 'fixture:%'),
            'reservations',(SELECT jsonb_agg(key ORDER BY key) FROM app_state WHERE key LIKE 'insight:last:%')
         )::text",
    ).fetch_one(pool).await?;
    Ok(serde_json::from_str(&value)?)
}
