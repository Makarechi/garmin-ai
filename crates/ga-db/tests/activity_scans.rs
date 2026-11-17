use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    activity_scans::{ActivityScanError, ActivityScansStarted, StartActivityScans},
};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "uses disposable TimescaleDB with pinned Python activity scan fixture"]
async fn keeps_active_scan_state_across_attempts_and_lanes() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL fixture URL"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    if !options
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Use a disposable database ending in _test".into());
    }
    let inspector = PgPool::connect_with(options).await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let steps: Vec<(String, String, u16, usize)> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_ACTIVITY_SCANS")?)?)?;
    let fingerprint = "a".repeat(64);
    let request = |now, days| StartActivityScans {
        authenticated_fingerprint: &fingerprint,
        timezone: "Europe/Bratislava",
        history_days: days,
        now,
    };
    let mut held = inspector.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *held)
        .await?;
    assert_eq!(
        db.start_activity_scans(request(steps[0].1.parse()?, 0))
            .await?,
        ActivityScansStarted::Busy
    );
    held.rollback().await?;
    assert!(matches!(
        db.start_activity_scans(StartActivityScans {
            authenticated_fingerprint: &"b".repeat(64),
            ..request(steps[0].1.parse()?, 0)
        })
        .await,
        Err(ActivityScanError::Owner(_))
    ));

    for (case, at, horizon, expected) in steps {
        match case.as_str() {
            "exhausted" => {
                sqlx::query(
                    "UPDATE jobs SET status='failed' WHERE right(payload->>'scan_key',7)=concat(chr(58),'recent')",
                )
                .execute(&inspector)
                .await?;
            }
            "cooldown" => {
                sqlx::query(
                    "UPDATE app_state SET value=value || jsonb_build_object(
                        'status','complete','next_scan_at','2026-10-26T00:00:00+00:00')
                     WHERE starts_with(key,'activity_scan:') AND right(key,7)=concat(chr(58),'recent')",
                )
                .execute(&inspector)
                .await?;
            }
            "disabled" => {
                sqlx::query(
                    "INSERT INTO module_configs(id,owner_id,pack_key,revision,tracking_enabled,
                        collection_enabled,reminders_enabled,visible,llm_enabled,settings)
                     SELECT gen_random_uuid(),id,'training',1,false,false,false,false,false,'{}'
                     FROM people",
                )
                .execute(&inspector)
                .await?;
            }
            _ => {}
        }
        let restarted = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
        let result = restarted
            .start_activity_scans(request(at.parse()?, horizon))
            .await?;
        if case == "disabled" {
            assert_eq!(result, ActivityScansStarted::CollectionDisabled);
        } else {
            assert_eq!(
                result,
                ActivityScansStarted::Scheduled { pages: expected },
                "{case}"
            );
        }
    }
    Ok(())
}
