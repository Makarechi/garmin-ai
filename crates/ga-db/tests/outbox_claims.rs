use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    outbox_claims::{ReplyClaimError, ReplyDestination},
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

const FIRST: Uuid = Uuid::from_u128(1001);
const CONVERSATION: Uuid = Uuid::from_u128(180);
const OPERATION: Uuid = Uuid::from_u128(182);

#[derive(Deserialize)]
struct Fixture {
    outbox: Value,
    checks: Vec<Check>,
}

#[derive(Deserialize)]
struct Check {
    name: String,
    now: String,
    seconds: u32,
    result: Option<String>,
    rows: Value,
}

#[tokio::test]
#[ignore = "compares outbox claims with pinned Python using disposable TimescaleDB"]
async fn claims_only_current_destinations_and_revisions() -> Result<(), Box<dyn Error>> {
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
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, current);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let fixture: Fixture = serde_json::from_slice(&fs::read(env::var("GA_TEST_OUTBOX_CLAIMS")?)?)?;
    seed(&pool, owner_id, &fixture.outbox).await?;
    let destination = || ReplyDestination {
        owner_id,
        channel: "fixture",
        instance_id: "primary",
    };
    let now: StoredInstant = "2026-11-05T10:00:00Z".parse()?;
    let initial = snapshot(&pool).await?;

    let mut legacy_worker = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *legacy_worker)
        .await?;
    assert!(matches!(
        database.claim_outbox_reply(destination(), now, 120).await,
        Err(ReplyClaimError::Busy)
    ));
    legacy_worker.rollback().await?;

    assert!(
        database
            .claim_outbox_reply(
                ReplyDestination {
                    owner_id: Uuid::from_u128(92),
                    ..destination()
                },
                now,
                120
            )
            .await?
            .is_none()
    );
    sqlx::raw_sql(
        "CREATE FUNCTION fail_synthetic_outbox_claim() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN RAISE EXCEPTION 'synthetic write failure'; END $$;
         CREATE TRIGGER fail_synthetic_outbox_claim BEFORE UPDATE ON outbox_messages
         FOR EACH ROW WHEN (NEW.state='sending') EXECUTE FUNCTION fail_synthetic_outbox_claim();",
    )
    .execute(&pool)
    .await?;
    assert!(matches!(
        database.claim_outbox_reply(destination(), now, 120).await,
        Err(ReplyClaimError::Database)
    ));
    assert_eq!(snapshot(&pool).await?, initial);
    sqlx::query("DROP TRIGGER fail_synthetic_outbox_claim ON outbox_messages")
        .execute(&pool)
        .await?;

    let count = fixture.checks.len();
    for check in fixture.checks {
        let actual = database
            .claim_outbox_reply(destination(), check.now.parse()?, check.seconds)
            .await;
        let result = match actual {
            Ok(Some(claim)) => {
                let token: Option<Uuid> =
                    sqlx::query_scalar("SELECT lease_token FROM outbox_messages WHERE id=$1")
                        .bind(claim.outbox_id)
                        .fetch_one(&pool)
                        .await?;
                assert_eq!(token, Some(claim.lease_token));
                Some(claim.outbox_id.to_string())
            }
            Ok(None) => None,
            Err(ReplyClaimError::InvalidRequest) => Some("InvalidRequest".into()),
            Err(error) => return Err(format!("{}: {error}", check.name).into()),
        };
        assert_eq!(result, check.result, "{}", check.name);
        assert_eq!(snapshot(&pool).await?, check.rows, "{}", check.name);
    }
    let unclaimed: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM outbox_messages WHERE id<$1 AND state='queued' AND attempts=0 AND lease_token IS NULL",
    )
    .bind(FIRST)
    .fetch_one(&pool)
    .await?;
    assert_eq!(unclaimed, 4);
    println!("{count} paired claims and queue snapshots matched");
    Ok(())
}

async fn seed(pool: &PgPool, owner: Uuid, outbox: &Value) -> Result<(), sqlx::Error> {
    for (id, instance, epoch) in [(180, "primary", 183), (181, "other", 184)] {
        sqlx::query(
            "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
             VALUES($1,$2,'fixture',$3,$4,'{}',true)",
        )
        .bind(Uuid::from_u128(id))
        .bind(owner)
        .bind(instance)
        .bind(Uuid::from_u128(epoch))
        .execute(pool)
        .await?;
    }
    for (id, revision, legacy) in [(201, 1, None), (202, 2, None), (203, 1, Some(40_i64))] {
        sqlx::query(
            "INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,external_event_id,
                        sender_ref,received_at,kind,envelope,revision,status,operation_id,legacy_telegram_update_id)
             VALUES($1,$2,$3,'fixture','primary',$4,'synthetic-owner','2026-11-05T10:00:00Z','text',
                        '{}',$5,'processed',$6,$7)",
        )
        .bind(Uuid::from_u128(id))
        .bind(owner)
        .bind(CONVERSATION)
        .bind(format!("synthetic-{id}"))
        .bind(revision)
        .bind(OPERATION)
        .bind(legacy)
        .execute(pool)
        .await?;
    }
    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,dedup_key,intent,memory_fence,
                     inbound_message_id,state,attempts,next_attempt_at,created_at)
         SELECT id,$1,conversation_id,operation_id,dedup_key,jsonb_set(intent,'{owner_id}',to_jsonb(($1::uuid)::text)),
                memory_fence,inbound_message_id,'queued',0,next_attempt_at,'2026-11-05T09:00:00Z'
         FROM jsonb_to_recordset($2::jsonb) AS r(id uuid, conversation_id uuid, operation_id uuid, dedup_key text,
              intent jsonb,memory_fence jsonb,inbound_message_id uuid,next_attempt_at timestamptz)",
    )
    .bind(owner)
    .bind(outbox.to_string())
    .execute(pool)
    .await?;
    // An explicitly written None in the legacy JSONB column is a JSON null.
    sqlx::query("UPDATE outbox_messages SET memory_fence='null'::jsonb WHERE id=$1")
        .bind(Uuid::from_u128(1005))
        .execute(pool)
        .await?;
    for (id, initiative, legacy_key, legacy_inbound, foreign) in [
        (901, true, None, None, false),
        (902, false, Some("synthetic-legacy-alias"), None, false),
        (903, false, None, Some(Uuid::from_u128(203)), false),
        (904, false, None, None, true),
    ] {
        let id = Uuid::from_u128(id);
        let binding = if foreign { Uuid::from_u128(999) } else { owner };
        let intent = json!({
            "intent_id": id.to_string(), "owner_id": binding.to_string(), "conversation_id": CONVERSATION.to_string(),
            "channel_instance": {"channel":"fixture","instance_id":"primary"},
            "initiative": initiative, "blocks":[{"text":"synthetic unrelated"}],
        });
        sqlx::query(
            "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,dedup_key,intent,
                           state,attempts,legacy_key,inbound_message_id,created_at)
             VALUES($1,$2,$3,$4,$5,$6::jsonb,'queued',0,$7,$8,'2026-11-05T08:00:00Z')",
        )
        .bind(id)
        .bind(owner)
        .bind(CONVERSATION)
        .bind(OPERATION)
        .bind(format!("isolated-outbox:{id}"))
        .bind(intent.to_string())
        .bind(legacy_key)
        .bind(legacy_inbound)
        .execute(pool)
        .await?;
    }
    Ok(())
}

async fn snapshot(pool: &PgPool) -> Result<Value, Box<dyn Error>> {
    let raw: String = sqlx::query_scalar(
        "SELECT jsonb_agg(jsonb_build_array(id,state,attempts,lease_token IS NOT NULL,lease_until) ORDER BY id)::text
         FROM outbox_messages WHERE id >= $1",
    )
    .bind(FIRST)
    .fetch_one(pool)
    .await?;
    Ok(serde_json::from_str(&raw)?)
}
