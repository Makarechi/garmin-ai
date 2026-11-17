use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    garmin_periodic::{GarminSyncSchedule, GarminSyncScheduleError, ScheduleGarminSync},
};
use serde::Deserialize;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const SOURCE: &str = "source:garmin:primary";

#[derive(Deserialize)]
struct Case {
    name: String,
    at: String,
    timezone: String,
    jobs: Vec<Value>,
    activity: Value,
    windows: Value,
}

#[tokio::test]
#[ignore = "uses the pinned Python runtime fixture and disposable TimescaleDB"]
async fn matches_pinned_periodic_garmin_schedule() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable test database")?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(target, actual);
    sqlx::query("INSERT INTO app_state(key,value) VALUES('account:garmin',jsonb_build_object('fingerprint',$1::text))")
        .bind(ACCOUNT).execute(&inspect).await?;
    let cases: Vec<Case> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GARMIN_PERIODIC")?)?)?;
    let initial = &cases[0];
    let request = ScheduleGarminSync {
        authenticated_fingerprint: ACCOUNT,
        source_instance: SOURCE,
        configured_timezone: &initial.timezone,
        history_days: 2,
        now: initial.at.parse()?,
    };
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let mut other = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *other)
        .await?;
    assert_eq!(
        db.schedule_garmin_sync(ScheduleGarminSync { ..request })
            .await?,
        GarminSyncSchedule::Busy
    );
    other.rollback().await?;
    assert!(matches!(
        db.schedule_garmin_sync(ScheduleGarminSync {
            authenticated_fingerprint: &"b".repeat(64),
            ..request
        })
        .await,
        Err(GarminSyncScheduleError::Owner(_))
    ));
    sqlx::query("ALTER TABLE jobs ADD CONSTRAINT synthetic_fail_periodic CHECK (dedup_key NOT LIKE 'daily:%')")
        .execute(&inspect).await?;
    assert!(matches!(
        db.schedule_garmin_sync(request).await,
        Err(GarminSyncScheduleError::Database)
    ));
    let untouched: bool = sqlx::query_scalar(
        "SELECT NOT EXISTS(SELECT 1 FROM jobs)
           AND NOT EXISTS(SELECT 1 FROM app_state WHERE split_part(key,':',1)
                         IN ('activity_scan','syncwindow','syncplan'))",
    )
    .fetch_one(&inspect)
    .await?;
    assert!(
        untouched,
        "Failed publication must leave prior scheduling state intact"
    );
    sqlx::query("ALTER TABLE jobs DROP CONSTRAINT synthetic_fail_periodic")
        .execute(&inspect)
        .await?;

    for case in cases {
        prepare(&inspect, &case.name).await?;
        let restarted = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
        let result = restarted
            .schedule_garmin_sync(ScheduleGarminSync {
                authenticated_fingerprint: ACCOUNT,
                source_instance: SOURCE,
                configured_timezone: &case.timezone,
                history_days: 2,
                now: case.at.parse()?,
            })
            .await
            .map_err(|error| format!("{}: {error} ({error:?})", case.name))?;
        match case.name.as_str() {
            "paused" => assert_eq!(result, GarminSyncSchedule::Paused),
            "disabled" => {
                assert!(matches!(result, GarminSyncSchedule::Disabled { retired } if retired > 0))
            }
            _ => assert!(matches!(result, GarminSyncSchedule::Scheduled { .. })),
        }
        let stored: String = sqlx::query_scalar(
            "SELECT coalesce(jsonb_agg(jsonb_build_array(dedup_key,payload,status,last_error,
                      (extract(epoch FROM run_at)*1000000)::bigint,
                      (extract(epoch FROM completed_at)*1000000)::bigint) ORDER BY dedup_key),'[]')::text
             FROM jobs WHERE kind='garmin_endpoint'
               AND split_part(dedup_key,':',1) IN ('frequent','morning','reconcile','daily')",
        ).fetch_one(&inspect).await?;
        let jobs: Vec<Value> = serde_json::from_str(&stored)?;
        assert_eq!(
            jobs.len(),
            case.jobs.len(),
            "{} periodic row count",
            case.name
        );
        for (actual, expected) in jobs.iter().zip(case.jobs) {
            let key = expected[0].as_str().ok_or("Missing fixture key")?;
            assert_eq!(
                actual
                    .as_array()
                    .and_then(|row| row.get(..4))
                    .ok_or("Missing stored job fields")?,
                expected
                    .as_array()
                    .and_then(|row| row.get(..4))
                    .ok_or("Missing expected job fields")?,
                "{} {key}",
                case.name
            );
            assert_eq!(actual[5], expected[5], "{} {key} completion", case.name);
            let delay = actual[4].as_i64().ok_or("Missing actual schedule time")?
                - expected[4]
                    .as_i64()
                    .ok_or("Missing expected schedule time")?;
            let max_jitter = if key.starts_with("frequent:") {
                60_000_000
            } else if key.starts_with("daily:") {
                120_000_000
            } else {
                0
            };
            assert!(
                (0..=max_jitter).contains(&delay),
                "{} {key} delay {delay}",
                case.name
            );
        }
        let activity = snapshot(
            &inspect,
            "SELECT coalesce(jsonb_agg(jsonb_build_array(split_part(payload->>'scan_key',':',3),
                  payload->>'since',status) ORDER BY payload->>'scan_key'),'[]')::text
             FROM jobs WHERE kind='garmin_activities' AND starts_with(dedup_key,'activity-page:')",
        )
        .await?;
        assert_eq!(activity, case.activity, "{} activity cursors", case.name);
        let windows = snapshot(&inspect,
            "SELECT coalesce(jsonb_agg(jsonb_build_array(key,value->>'status') ORDER BY key),'[]')::text
             FROM app_state WHERE starts_with(key,'syncwindow:')").await?;
        assert_eq!(windows, case.windows, "{} history windows", case.name);
        println!("{} matched {} periodic jobs", case.name, jobs.len());
    }
    Ok(())
}

async fn snapshot(pool: &PgPool, query: &'static str) -> Result<Value, Box<dyn Error>> {
    let text: String = sqlx::query_scalar(query).fetch_one(pool).await?;
    Ok(serde_json::from_str(&text)?)
}

async fn prepare(pool: &PgPool, name: &str) -> Result<(), sqlx::Error> {
    const ONBOARD: &str = "INSERT INTO app_state(key,value)
        VALUES('preferences:onboarding',jsonb_build_object('source_instance_ids',jsonb_build_array('source:garmin:primary')))
        ON CONFLICT (key) DO UPDATE SET value=excluded.value";
    let statements: &[&str] = match name {
        "stored_zone_and_wake" => &[
            r#"INSERT INTO timeline_intervals(id,start,"end",label,source,confidence,confirmed,evidence)
               VALUES('synthetic-wake','2026-10-26T08:00:00+00:00','2026-10-26T13:00:00+00:00',
                      'sleep','fixture',1,false,'{}')"#,
            ONBOARD,
        ],
        "paused" => &["INSERT INTO app_state(key,value)
            VALUES('integration:garmin',jsonb_build_object('status','reauth_required'))"],
        "disabled" => &["UPDATE app_state SET value=jsonb_build_object('source_instance_ids',jsonb_build_array())
                         WHERE key='preferences:onboarding'"],
        "restricted" => &[
            ONBOARD,
            "DELETE FROM app_state WHERE key='integration:garmin'",
            "INSERT INTO module_configs(id,owner_id,pack_key,revision,tracking_enabled,
            collection_enabled,reminders_enabled,visible,llm_enabled,settings)
            SELECT gen_random_uuid(),id,'wellbeing',1,false,true,false,false,false,'{}' FROM people",
        ],
        _ => &[],
    };
    for &statement in statements {
        sqlx::query(statement).execute(pool).await?;
    }
    Ok(())
}
