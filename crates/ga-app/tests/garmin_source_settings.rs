use std::{env, error::Error, fs, str::FromStr};

use ga_app::garmin_worker::{GarminConfigurationError, GarminSourceSettings};
use ga_archive::GarminTokenFile;
use ga_db::{
    Database,
    garmin_integration::{
        GarminIntegrationError, GarminRequestPause, GarminSourceAdmission, GarminTransportOutcome,
    },
    garmin_owner::OwnerVerificationError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;

const PRIMARY: &str = "source:garmin:primary";
const EXPLICIT: &str = "source:garmin:archive";
const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[test]
fn worker_discovers_new_legacy_credentials_without_restarting() -> Result<(), Box<dyn Error>> {
    let root = tempfile::tempdir()?;
    let directory = root.path().canonicalize()?.join("tokens");
    let source = GarminSourceSettings::for_worker(None, &directory)?
        .ok_or("Expected implicit Garmin source")?;
    assert_eq!(source.instance_id(), PRIMARY);
    assert!(source.existing_tokens()?.is_none());
    assert!(!directory.exists());

    GarminTokenFile::open(&directory)?
        .try_lock()?
        .replace(b"synthetic token file")?;
    assert!(source.existing_tokens()?.is_some());
    assert!(GarminSourceSettings::for_worker(Some("[]"), &directory)?.is_none());
    Ok(())
}

#[test]
#[ignore = "uses the source selection cases recorded by pinned Python"]
fn matches_the_selected_garmin_source_and_opens_only_existing_tokens() -> Result<(), Box<dyn Error>>
{
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GARMIN_SOURCE_SETTINGS")?)?)?;
    let root = tempfile::tempdir()?;
    let root_path = root.path().canonicalize()?;
    let saved = root_path.join("saved");
    let absent = root_path.join("absent");
    GarminTokenFile::open(&saved)?
        .try_lock()?
        .replace(b"synthetic token file")?;
    for case in cases {
        let dir = if case["has_tokens"] == true {
            &saved
        } else {
            &absent
        };
        let actual = GarminSourceSettings::resolve(case["integrations"].as_str(), dir)?;
        assert_eq!(
            actual.as_ref().map(GarminSourceSettings::instance_id),
            case["selected"].as_str(),
            "{}",
            case["name"],
        );
        if let Some(actual) = actual {
            assert_eq!(
                actual.existing_tokens()?.is_some(),
                case["has_tokens"] == true
            );
        }
    }
    assert!(!absent.exists());
    let broken = root_path.join("broken");
    GarminTokenFile::open(&broken)?;
    std::os::unix::fs::symlink(
        saved.join("garmin_tokens.json"),
        broken.join("garmin_tokens.json"),
    )?;
    assert!(GarminSourceSettings::resolve(Some("[]"), &broken)?.is_none());
    assert!(matches!(
        GarminSourceSettings::resolve(None, &broken),
        Err(GarminConfigurationError::TokenDirectory(_)),
    ));
    Ok(())
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and its saved Garmin binding and policy"]
async fn checks_current_source_selection_binding_and_cooldown_before_transport()
-> Result<(), Box<dyn Error>> {
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
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, target);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let wall: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(wall)?;
    assert!(matches!(
        db.garmin_source_admission(PRIMARY, now).await?,
        GarminSourceAdmission::Unbound
    ));

    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES
            ('account:garmin',jsonb_build_object('fingerprint',$1::text)),
            ('preferences:onboarding','{\"source_instance_ids\":[\"source:garmin:archive\"]}')",
    )
    .bind(ACCOUNT)
    .execute(&pool)
    .await?;
    assert!(matches!(
        db.garmin_source_admission(PRIMARY, now).await?,
        GarminSourceAdmission::Unselected
    ));
    assert!(matches!(
        db.garmin_source_admission(EXPLICIT, now).await?,
        GarminSourceAdmission::Ready { fingerprint } if fingerprint == ACCOUNT
    ));

    db.record_garmin_transport(
        ACCOUNT,
        now,
        GarminTransportOutcome::RateLimited {
            retry_after: Some(std::time::Duration::from_secs(90)),
        },
    )
    .await?;
    assert!(matches!(
        db.garmin_source_admission(EXPLICIT,now).await?,
        GarminSourceAdmission::Paused(GarminRequestPause::Cooldown(until)) if until > now,
    ));
    let after = StoredInstant::try_from(wall + time::Duration::minutes(16))?;
    assert!(matches!(
        db.garmin_source_admission(EXPLICIT, after).await?,
        GarminSourceAdmission::Ready { .. }
    ));
    db.record_garmin_transport(
        ACCOUNT,
        after,
        GarminTransportOutcome::AuthenticationRequired,
    )
    .await?;
    assert!(matches!(
        db.garmin_source_admission(EXPLICIT, after).await?,
        GarminSourceAdmission::Paused(GarminRequestPause::Reauthentication),
    ));

    let mut update = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621)")
        .execute(&mut *update)
        .await?;
    assert!(matches!(
        db.garmin_source_admission(EXPLICIT, after).await,
        Err(GarminIntegrationError::Busy)
    ));
    update.rollback().await?;
    sqlx::query("UPDATE app_state SET value='{\"fingerprint\":false}' WHERE key='account:garmin'")
        .execute(&pool)
        .await?;
    assert!(matches!(
        db.garmin_source_admission(EXPLICIT, after).await,
        Err(GarminIntegrationError::Owner(
            OwnerVerificationError::InvalidStoredBinding
        )),
    ));
    Ok(())
}
