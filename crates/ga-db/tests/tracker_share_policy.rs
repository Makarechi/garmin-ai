use std::{env, error::Error, fs, io, str::FromStr, time::Duration};

use ga_db::{
    Database,
    share_policy::{
        DestinationKind, ShareCategory, ShareDestination, SharePolicyError, ShareRequirement,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

#[derive(Deserialize)]
struct Decision {
    version: String,
    destination_kind: DestinationKind,
    instance: String,
    categories: Vec<ShareCategory>,
    allowed: bool,
}

const SENSITIVE: Uuid = Uuid::from_u128(2);
const DEFINITION: Uuid = Uuid::from_u128(44);
const CHANNEL: &str = "telegram:primary";
const CHANNEL_LOCK: i64 = 72104631;
const REPLAY_LOCK: i64 = 72104619;

#[tokio::test]
#[ignore = "requires the synthetic tracker consent fixture on disposable TimescaleDB"]
async fn verifies_recorded_consent_and_fences_delivery() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected disposable TimescaleDB URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Database must end in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(&actual, target);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let cases: Vec<Decision> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TRACKER_SHARE_POLICY")?)?)?;
    for (index, test) in cases.into_iter().enumerate() {
        let requirement = ShareRequirement {
            version_id: test.version.parse()?,
            categories: &test.categories,
        };
        let destination = ShareDestination::new(test.destination_kind, &test.instance)?;
        assert_eq!(
            db.tracker_version_allowed(requirement, destination).await?,
            test.allowed,
            "case {index}"
        );
    }

    assert!(matches!(
        ShareDestination::new(DestinationKind::Channel, "Telegram/other"),
        Err(SharePolicyError::InvalidDestination)
    ));
    let destination = ShareDestination::new(DestinationKind::Channel, CHANNEL)?;
    let required = [ShareRequirement {
        version_id: SENSITIVE,
        categories: &[ShareCategory::Facts, ShareCategory::Schema],
    }];

    let called = std::cell::Cell::new(false);
    let mut mutation = inspect.begin().await?;
    let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
        .bind(CHANNEL_LOCK)
        .fetch_one(&mut *mutation)
        .await?;
    assert!(locked);
    assert!(matches!(
        db.with_tracker_delivery(destination, &required, || async { called.set(true) })
            .await,
        Err(SharePolicyError::Busy)
    ));
    mutation.rollback().await?;
    assert!(!called.get());

    let delivered = db
        .with_tracker_delivery(destination, &required, || async {
            let mut mutation = inspect.begin().await?;
            for lock in [REPLAY_LOCK, CHANNEL_LOCK] {
                let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                    .bind(lock)
                    .fetch_one(&mut *mutation)
                    .await?;
                assert!(!acquired);
            }
            mutation.rollback().await?;
            Ok::<_, sqlx::Error>("synthetic delivery")
        })
        .await?;
    assert_eq!(delivered.transpose()?, Some("synthetic delivery"));

    let started = std::cell::Cell::new(false);
    assert!(
        tokio::time::timeout(
            Duration::from_millis(300),
            db.with_tracker_delivery(destination, &required, || async {
                started.set(true);
                std::future::pending::<()>().await;
            }),
        )
        .await
        .is_err()
    );
    assert!(started.get());

    // A cancelled delivery releases both locks before a later revocation.
    let mut revoke = inspect.begin().await?;
    sqlx::query("SET LOCAL statement_timeout = '750ms'")
        .execute(&mut *revoke)
        .await?;
    for lock in [REPLAY_LOCK, CHANNEL_LOCK] {
        sqlx::query("SELECT pg_advisory_xact_lock($1)")
            .bind(lock)
            .execute(&mut *revoke)
            .await?;
    }
    sqlx::query("DELETE FROM app_state WHERE key=$1")
        .bind(format!("tracker-consent:{DEFINITION}:channel:{CHANNEL}"))
        .execute(&mut *revoke)
        .await?;
    revoke.commit().await?;
    assert_eq!(
        db.with_tracker_delivery(destination, &required, || async { called.set(true) })
            .await?,
        None::<()>,
    );

    assert!(!called.get());
    let timestamp: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&inspect)
        .await?;
    let timestamp = StoredInstant::try_from(timestamp)?;
    sqlx::query(
        "INSERT INTO app_state(key,value)
         SELECT concat('tracker-consent:',definition_id,':channel:telegram:primary'),
             jsonb_build_object('definition_id',definition_id::text,'destination_kind','channel',
                 'destination_instance_id','telegram:another','policy_revision',1,
                 'granted_at',$2::text,'categories','[\"schema\",\"facts\"]'::jsonb)
         FROM event_definition_versions WHERE id=$1",
    )
    .bind(SENSITIVE)
    .bind(timestamp.to_string())
    .execute(&inspect)
    .await?;
    assert!(!db.tracker_version_allowed(required[0], destination).await?);
    Ok(())
}
