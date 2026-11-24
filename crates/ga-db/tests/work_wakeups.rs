use std::{env, error::Error, io::Cursor, str::FromStr, time::Duration};

use ga_db::{
    Database, WorkSubscription, WorkTopic,
    database_restore::DatabaseRestoreError,
    jobs::JobStoreError,
    schema_migration::{SchemaMigrationError, SchemaState},
};
use ga_domain::timestamp::StoredInstant;
use sqlx::{
    ConnectOptions, PgPool,
    postgres::{PgConnectOptions, PgListener},
};
use time::OffsetDateTime;
use tokio::time::{sleep, timeout};
use uuid::Uuid;

const CHANNEL: &str = "ga_work_ready";

async fn notification(listener: &mut PgListener) -> Result<String, Box<dyn Error>> {
    Ok(timeout(Duration::from_secs(2), listener.recv())
        .await??
        .payload()
        .to_owned())
}

async fn quiet(listener: &mut PgListener) {
    assert!(
        timeout(Duration::from_millis(60), listener.recv())
            .await
            .is_err()
    );
}

async fn insert_job(pool: &PgPool, kind: &str, delay_ms: i64) -> Result<Uuid, sqlx::Error> {
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES($1,$2,'{}',$1::text,'pending',clock_timestamp()+$3*interval '1 millisecond',0)",
    )
    .bind(id)
    .bind(kind)
    .bind(delay_ms)
    .execute(pool)
    .await?;
    Ok(id)
}

async fn wait_until_job_due(
    pool: &PgPool,
    subscription: &mut WorkSubscription,
    id: Uuid,
) -> Result<(), Box<dyn Error>> {
    timeout(Duration::from_secs(3), async {
        loop {
            let due: bool =
                sqlx::query_scalar("SELECT run_at<=clock_timestamp() FROM jobs WHERE id=$1")
                    .bind(id)
                    .fetch_one(pool)
                    .await?;
            if due {
                return Ok::<_, Box<dyn Error>>(());
            }
            assert!(
                subscription.wait(Duration::from_secs(2)).await,
                "Missing scheduled wakeup"
            );
        }
    })
    .await??;
    Ok(())
}

async fn verify_upgrade_and_restore(
    database: &Database,
    pool: &PgPool,
) -> Result<(), Box<dyn Error>> {
    assert_eq!(database.schema_status().await?, SchemaState::Current);
    // Recreate the immediately preceding schema in this disposable database.
    // Only functions/triggers differ; no exported row or table changes.
    let mut before = pool.begin().await?;
    sqlx::raw_sql(
        "DROP TRIGGER ga_jobs_work_wakeup ON jobs;
         DROP TRIGGER ga_outbox_work_wakeup ON outbox_messages;
         DROP FUNCTION ga_notify_job_work(); DROP FUNCTION ga_notify_outbox_work();
         DROP FUNCTION ga_work_topic(text);
         UPDATE alembic_version SET version_num='c54f8a72d130';",
    )
    .execute(&mut *before)
    .await?;
    before.commit().await?;
    assert_eq!(
        database.schema_status().await?,
        SchemaState::BeforeQueueWakeups
    );
    let mut active = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104620)")
        .execute(&mut *active)
        .await?;
    assert!(matches!(
        database.migrate_schema().await,
        Err(SchemaMigrationError::Busy)
    ));
    active.rollback().await?;
    assert_eq!(
        database.migrate_schema().await?,
        SchemaState::BeforeQueueWakeups
    );
    assert_eq!(database.migrate_schema().await?, SchemaState::Current);

    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(pool)
        .await?;
    let at = StoredInstant::try_from(
        sqlx::query_scalar::<_, OffsetDateTime>("SELECT clock_timestamp()")
            .fetch_one(pool)
            .await?,
    )?;
    let mut output = Vec::new();
    let counts = database
        .export_database_rows(owner, at, &mut output)
        .await?;
    let current = String::from_utf8(output)?;
    let previous = current.replacen("d1f7a9306e42", "c54f8a72d130", 1);
    assert_ne!(current, previous);
    // The destination stays current, while both identical row contracts restore.
    assert_eq!(
        database
            .restore_current_database(&mut Cursor::new(&previous))
            .await?,
        counts
    );
    assert_eq!(
        database
            .restore_current_database(&mut Cursor::new(&current))
            .await?,
        counts
    );
    let older = previous.replacen("c54f8a72d130", "c8f51d3a7e20", 1);
    assert!(matches!(
        database
            .restore_current_database(&mut Cursor::new(older))
            .await,
        Err(DatabaseRestoreError::Revision)
    ));
    Ok(())
}

async fn verify_transactional_hints(pool: &PgPool) -> Result<(), Box<dyn Error>> {
    let mut listener = PgListener::connect_with(pool).await?;
    listener.listen(CHANNEL).await?;
    let mut rolled_back = pool.begin().await?;
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         VALUES(gen_random_uuid(),'raw_replay','{}','synthetic-rollback','pending',now(),0)",
    )
    .execute(&mut *rolled_back)
    .await?;
    quiet(&mut listener).await;
    rolled_back.rollback().await?;
    quiet(&mut listener).await;

    let one = Uuid::new_v4();
    let two = Uuid::new_v4();
    let mut committed = pool.begin().await?;
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         SELECT id,'raw_replay','{}',id::text,'pending',now(),0 FROM unnest($1::uuid[]) ids(id)",
    )
    .bind([one, two])
    .execute(&mut *committed)
    .await?;
    quiet(&mut listener).await;
    committed.commit().await?;
    assert_eq!(notification(&mut listener).await?, "replay");
    quiet(&mut listener).await; // Identical hints are coalesced in one transaction.

    sqlx::query("UPDATE jobs SET status='running',lease_token=gen_random_uuid(),lease_until=now()+interval '1 minute' WHERE id=$1")
        .bind(one).execute(pool).await?;
    assert_eq!(notification(&mut listener).await?, "deadlines");
    sqlx::query("UPDATE jobs SET lease_until=lease_until+interval '1 minute' WHERE id=$1")
        .bind(one)
        .execute(pool)
        .await?;
    sqlx::query("UPDATE jobs SET attempts=attempts+1 WHERE id=$1")
        .bind(two)
        .execute(pool)
        .await?;
    sqlx::query("UPDATE jobs SET status='running' WHERE id=$1")
        .bind(Uuid::nil())
        .execute(pool)
        .await?;
    quiet(&mut listener).await; // Renewals, bookkeeping and zero-row claims stay quiet.
    sqlx::query("UPDATE jobs SET status='pending',lease_token=NULL,lease_until=NULL,payload=jsonb_build_object('local_safety_checked',true) WHERE id=$1")
        .bind(one).execute(pool).await?;
    assert_eq!(notification(&mut listener).await?, "replay");

    let inbound = insert_job(pool, "telegram_native_inbound", 0).await?;
    let mut topics = vec![
        notification(&mut listener).await?,
        notification(&mut listener).await?,
    ];
    topics.sort();
    assert_eq!(topics, ["garmin_schedule", "telegram_inbound"]);
    sqlx::query(
        "UPDATE jobs SET payload=jsonb_build_object('local_safety_checked',true) WHERE id=$1",
    )
    .bind(inbound)
    .execute(pool)
    .await?;
    assert_eq!(notification(&mut listener).await?, "telegram_inbound");
    quiet(&mut listener).await; // A processing handoff does not re-plan Garmin.
    sqlx::query("UPDATE jobs SET status='done' WHERE id=$1")
        .bind(inbound)
        .execute(pool)
        .await?;
    assert_eq!(notification(&mut listener).await?, "telegram_inbound");
    drop(listener);
    sqlx::query("DELETE FROM jobs").execute(pool).await?;
    Ok(())
}

async fn verify_busy_is_not_empty(
    database: &Database,
    pool: &PgPool,
) -> Result<(), Box<dyn Error>> {
    let now = sqlx::query_scalar::<_, OffsetDateTime>("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    let at = StoredInstant::try_from(now)?;
    let mut active = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104624)")
        .execute(&mut *active)
        .await?;
    assert!(matches!(
        database.claim_garmin_job(at, now.date(), 30, false).await,
        Err(JobStoreError::Busy)
    ));
    assert!(matches!(
        database.claim_replay_job(at, 30).await,
        Err(JobStoreError::Busy)
    ));
    assert!(matches!(
        database.claim_daily_backup(at, 30).await,
        Err(JobStoreError::Busy)
    ));
    assert!(matches!(
        database.claim_storage_check(at, 30).await,
        Err(JobStoreError::Busy)
    ));
    active.rollback().await?;
    assert!(
        database
            .claim_garmin_job(at, now.date(), 30, false)
            .await?
            .is_none()
    );
    assert!(database.claim_replay_job(at, 30).await?.is_none());
    assert!(database.claim_daily_backup(at, 30).await?.is_none());
    assert!(database.claim_storage_check(at, 30).await?.is_none());
    let mut active = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621)")
        .execute(&mut *active)
        .await?;
    assert!(matches!(
        database.schedule_source_plugin_polls(&[], at).await,
        Err(JobStoreError::Busy)
    ));
    assert!(matches!(
        database
            .claim_selected_garmin_job(at, now.date(), 30, false, "primary")
            .await,
        Err(JobStoreError::Busy)
    ));
    active.rollback().await?;
    Ok(())
}

async fn verify_deadlines_and_reconnect(
    database: &Database,
    pool: &PgPool,
) -> Result<(), Box<dyn Error>> {
    let mut replay = database.work_subscription(WorkTopic::Replay);
    let mut outbox = database.work_subscription(WorkTopic::TelegramOutbox);
    let worker = database.clone();
    let driver = tokio::spawn(async move { worker.run_work_wakeups().await });
    assert!(replay.wait(Duration::from_secs(2)).await);
    sleep(Duration::from_millis(80)).await;
    replay.take_pending();
    outbox.take_pending();

    // The immediate commit hint arrives before a future retry is eligible.
    let delayed = insert_job(pool, "raw_replay", 300).await?;
    wait_until_job_due(pool, &mut replay, delayed).await?;

    // A retry can cross its deadline inside the 25ms refresh-coalescing window.
    let near = insert_job(pool, "raw_replay", 10).await?;
    wait_until_job_due(pool, &mut replay, near).await?;

    // Only the first deadline per topic is cached. A second one can cross during
    // its refresh while the first due row remains unclaimable/unchanged.
    let first = insert_job(pool, "raw_replay", 200).await?;
    let second = insert_job(pool, "raw_replay", 210).await?;
    wait_until_job_due(pool, &mut replay, first).await?;
    wait_until_job_due(pool, &mut replay, second).await?;
    sleep(Duration::from_millis(80)).await;
    replay.take_pending();
    assert!(
        !replay.wait(Duration::from_millis(120)).await,
        "Overdue blocked rows must not spin"
    );

    let leased = insert_job(pool, "raw_replay", 0).await?;
    sqlx::query("UPDATE jobs SET status='running',lease_token=gen_random_uuid(),lease_until=clock_timestamp()+interval '300 milliseconds' WHERE id=$1")
        .bind(leased).execute(pool).await?;
    timeout(Duration::from_secs(3), async {
        loop {
            let expired: bool =
                sqlx::query_scalar("SELECT lease_until<clock_timestamp() FROM jobs WHERE id=$1")
                    .bind(leased)
                    .fetch_one(pool)
                    .await?;
            if expired {
                return Ok::<_, Box<dyn Error>>(());
            }
            assert!(
                replay.wait(Duration::from_secs(2)).await,
                "Missing expired lease wakeup"
            );
        }
    })
    .await??;

    let conversation = Uuid::new_v4();
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(pool)
        .await?;
    sqlx::query("INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch) VALUES($1,$2,'fixture','wakeups',gen_random_uuid())")
        .bind(conversation).bind(owner).execute(pool).await?;
    let outgoing = Uuid::new_v4();
    sqlx::query("INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,dedup_key,intent,state,next_attempt_at) VALUES($1,$2,$3,gen_random_uuid(),$1::text,'{}','queued',clock_timestamp()+interval '300 milliseconds')")
        .bind(outgoing).bind(owner).bind(conversation).execute(pool).await?;
    timeout(Duration::from_secs(3), async {
        loop {
            let due: bool = sqlx::query_scalar(
                "SELECT next_attempt_at<=clock_timestamp() FROM outbox_messages WHERE id=$1",
            )
            .bind(outgoing)
            .fetch_one(pool)
            .await?;
            if due {
                return Ok::<_, Box<dyn Error>>(());
            }
            assert!(
                outbox.wait(Duration::from_secs(2)).await,
                "Missing outbox retry wakeup"
            );
        }
    })
    .await??;

    sleep(Duration::from_millis(80)).await;
    replay.take_pending();
    let sessions: Vec<i32> = sqlx::query_scalar(
        "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND application_name='garmin-ai-work-wakeups'",
    ).fetch_all(pool).await?;
    assert_eq!(
        sessions.len(),
        1,
        "One shared dedicated listener connection"
    );
    sqlx::query("SELECT pg_terminate_backend($1)")
        .bind(sessions[0])
        .execute(pool)
        .await?;
    // The reconnect broadcast catches work even when its notification was lost.
    let after_loss = insert_job(pool, "raw_replay", 0).await?;
    assert!(replay.wait(Duration::from_secs(4)).await);
    let pending: bool = sqlx::query_scalar("SELECT status='pending' FROM jobs WHERE id=$1")
        .bind(after_loss)
        .fetch_one(pool)
        .await?;
    assert!(pending);
    driver.abort();
    let _ = driver.await;
    Ok(())
}

#[tokio::test]
#[ignore = "requires an isolated freshly migrated PostgreSQL/TimescaleDB database"]
async fn transactional_queue_wakeups_preserve_deadlines_and_recovery() -> Result<(), Box<dyn Error>>
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
        .ok_or("Expected disposable test database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        expected
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM jobs")
            .fetch_one(&pool)
            .await?,
        0
    );
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    verify_upgrade_and_restore(&database, &pool).await?;
    verify_busy_is_not_empty(&database, &pool).await?;
    verify_transactional_hints(&pool).await?;
    verify_deadlines_and_reconnect(&database, &pool).await?;
    Ok(())
}
