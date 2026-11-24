use std::{env, error::Error, str::FromStr};

use ga_db::{Database, garmin_periodic::ScheduleGarminSync};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[tokio::test]
#[ignore = "requires a freshly migrated disposable TimescaleDB"]
async fn refreshes_stale_data_once_per_telegram_conversation() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected a disposable test database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, actual);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('account:garmin',jsonb_build_object('fingerprint',$1::text))")
        .bind(ACCOUNT)
        .execute(&pool)
        .await?;
    sqlx::query(
        "UPDATE module_configs SET collection_enabled=true WHERE pack_key IN ('sleep','wellbeing')",
    )
    .execute(&pool)
    .await?;
    let conversation: Uuid = sqlx::query_scalar(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch)
         VALUES(gen_random_uuid(),$1,'telegram','primary',gen_random_uuid()) RETURNING id",
    )
    .bind(owner)
    .fetch_one(&pool)
    .await?;

    let request = |at: &str| -> Result<ScheduleGarminSync<'_>, Box<dyn Error>> {
        Ok(ScheduleGarminSync {
            authenticated_fingerprint: ACCOUNT,
            source_instance: "source:garmin:primary",
            configured_timezone: "UTC",
            history_days: 0,
            now: at.parse()?,
        })
    };

    let first = "2026-11-24T13:06:00Z";
    let opener = message(&pool, owner, conversation, first, first).await?;
    db.schedule_garmin_sync(request(first)?).await?;
    let initial = refresh_jobs(&pool).await?;
    assert_eq!(initial, [format!("telegram-refresh:{opener}:sleep")]);
    db.schedule_garmin_sync(request("2026-11-24T13:06:30Z")?)
        .await?;
    assert_eq!(refresh_jobs(&pool).await?, initial);

    sqlx::query(
        "UPDATE jobs SET status='done',completed_at='2026-11-24T13:06:35Z'
         WHERE dedup_key=$1",
    )
    .bind(&initial[0])
    .execute(&pool)
    .await?;
    let follow_up = "2026-11-24T13:07:00Z";
    message(&pool, owner, conversation, follow_up, follow_up).await?;
    db.schedule_garmin_sync(request(follow_up)?).await?;
    assert_eq!(refresh_jobs(&pool).await?, initial);

    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('ingest:garmin_connect:sleep:2026-11-24',
          '{\"fetched_at\":\"2026-11-24T13:22:30Z\"}')",
    )
    .execute(&pool)
    .await?;
    let fresh = "2026-11-24T13:23:00Z";
    message(&pool, owner, conversation, fresh, fresh).await?;
    db.schedule_garmin_sync(request(fresh)?).await?;
    assert_eq!(refresh_jobs(&pool).await?, initial);

    let later = "2026-11-24T13:42:00Z";
    let later_opener = message(&pool, owner, conversation, later, later).await?;
    db.schedule_garmin_sync(request(later)?).await?;
    let after_pause = refresh_jobs(&pool).await?;
    assert_eq!(after_pause.len(), 2);
    assert!(after_pause.contains(&format!("telegram-refresh:{later_opener}:sleep")));

    sqlx::query(
        "UPDATE jobs SET status='done',completed_at='2026-11-24T13:42:01Z' WHERE dedup_key=$1",
    )
    .bind(format!("telegram-refresh:{later_opener}:sleep"))
    .execute(&pool)
    .await?;
    let delayed = "2026-11-24T14:03:00Z";
    message(&pool, owner, conversation, "2026-11-24T13:30:00Z", delayed).await?;
    db.schedule_garmin_sync(request(delayed)?).await?;
    assert_eq!(refresh_jobs(&pool).await?, after_pause);

    let fresh_after_delayed = "2026-11-24T14:04:00Z";
    let new_opener = message(
        &pool,
        owner,
        conversation,
        fresh_after_delayed,
        fresh_after_delayed,
    )
    .await?;
    db.schedule_garmin_sync(request(fresh_after_delayed)?)
        .await?;
    let after_backlog = refresh_jobs(&pool).await?;
    assert_eq!(after_backlog.len(), 3);
    assert!(after_backlog.contains(&format!("telegram-refresh:{new_opener}:sleep")));
    Ok(())
}

async fn message(
    pool: &PgPool,
    owner: Uuid,
    conversation: Uuid,
    occurred: &str,
    received: &str,
) -> Result<Uuid, Box<dyn Error>> {
    let id = Uuid::new_v4();
    let occurred = OffsetDateTime::from(occurred.parse::<ga_domain::timestamp::StoredInstant>()?);
    let received = OffsetDateTime::from(received.parse::<ga_domain::timestamp::StoredInstant>()?);
    sqlx::query(
        "WITH saved AS (
           INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,
             external_event_id,sender_ref,occurred_at,received_at,kind,normalized_text,envelope,operation_id)
           VALUES($1,$2,$3,'telegram','primary',$1::uuid::text,'fixture',$4,$5,'text','Synthetic question','{}',gen_random_uuid())
           RETURNING id
         )
         INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
         SELECT gen_random_uuid(),'telegram_native_inbound','{}',concat('telegram:inbound:native:',id),'pending',$5,0 FROM saved",
    )
    .bind(id)
    .bind(owner)
    .bind(conversation)
    .bind(occurred)
    .bind(received)
    .execute(pool)
    .await?;
    Ok(id)
}

async fn refresh_jobs(pool: &PgPool) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar("SELECT dedup_key FROM jobs WHERE starts_with(dedup_key,'telegram-refresh:') ORDER BY dedup_key")
        .fetch_all(pool)
        .await
}
