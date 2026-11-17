use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    history_windows::{HistorySchedule, HistoryScheduleError, ScheduleHistory},
};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and the pinned Python scheduler fixture"]
async fn schedules_owner_history_through_restarts_and_collection_changes()
-> Result<(), Box<dyn Error>> {
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
    let fingerprint = "a".repeat(64);
    let cases: Vec<(String, String, u16, usize)> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_HISTORY_WINDOWS")?)?)?;
    let count = || sqlx::query_scalar::<_, i64>("SELECT count(*) FROM jobs").fetch_one(&inspector);
    let request = ScheduleHistory {
        authenticated_fingerprint: &fingerprint,
        timezone: "Europe/Bratislava",
        horizon_days: 5,
        now: cases[0].1.parse()?,
    };
    let mut concurrent = inspector.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *concurrent)
        .await?;
    assert_eq!(
        db.schedule_history(request).await?,
        HistorySchedule::default()
    );
    assert_eq!(count().await?, 0);
    concurrent.rollback().await?;
    let denied = ScheduleHistory {
        authenticated_fingerprint: &"b".repeat(64),
        ..request
    };
    assert!(matches!(
        db.schedule_history(denied).await,
        Err(HistoryScheduleError::Owner(_))
    ));

    for (name, at, horizon_days, expected) in cases {
        if name == "retry_first" {
            sqlx::query(
                "UPDATE app_state SET value=value || jsonb_build_object('status','disabled')
                 WHERE key IN (SELECT key FROM app_state WHERE starts_with(key,'syncwindow:')
                               ORDER BY key LIMIT 3)",
            )
            .execute(&inspector)
            .await?;
        }
        if name == "restrict" {
            sqlx::query(
                "INSERT INTO module_configs(id, owner_id, pack_key, revision, tracking_enabled,
                    collection_enabled, reminders_enabled, visible, llm_enabled, settings)
                 SELECT gen_random_uuid(), id, 'wellbeing',1,false,true,false,false,false,'{}'
                 FROM people",
            )
            .execute(&inspector)
            .await?;
        }
        let before = count().await?;
        let restarted = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
        let actual = restarted
            .schedule_history(ScheduleHistory {
                now: at.parse()?,
                horizon_days,
                ..request
            })
            .await?;
        assert_eq!(actual.queued + actual.reenabled, expected, "{name}");
        assert_eq!(
            usize::try_from(count().await? - before)?,
            expected,
            "{name}"
        );
    }
    Ok(())
}
