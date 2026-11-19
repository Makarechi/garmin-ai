use std::{env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    diary::actions::{TrackerTextContext, TrackerTextError},
};
use ga_domain::{
    access::{Scope, Scopes},
    timestamp::StoredInstant,
    tracker_nl::TrackerTextRequest,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses a fresh synthetic registry with at least 1000 configured trackers"]
async fn bounded_offline_tracker_forms_preserve_context_and_facts() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let options =
        PgConnectOptions::from_str(&url.replace("postgresql+psycopg://", "postgresql://"))?
            .disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected isolated test database")?
        .to_owned();
    let pool = PgPool::connect_with(options).await?;
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        expected
    );
    let configured: i64 = sqlx::query_scalar("SELECT count(*) FROM tracker_configs")
        .fetch_one(&pool)
        .await?;
    assert!(configured >= 1000);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid isolated connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let scopes: Scopes = [Scope::ReadDiary].into_iter().collect();
    let message_at = StoredInstant::from_legacy_input(&json!("2026-09-20T20:00:00+00:00"))
        .ok_or("Expected synthetic first message clock")?;
    let context = |timezone| TrackerTextContext {
        owner,
        scopes,
        actor: "api",
        message_at,
        configured_timezone: timezone,
        configured_locale: "en",
    };
    let before: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(&pool)
            .await?;
    for index in 0..12 {
        let text = if index % 2 == 0 {
            "unrelated"
        } else {
            "Synthetic Focus 50"
        };
        let request = TrackerTextRequest::from_value(
            &json!({"text":text,"operation_id":format!("synthetic-scale-{index}")}),
        )?;
        let result = database
            .tracker_text_fallback(&request, context("UTC"))
            .await?;
        assert_eq!(
            result["forms"]
                .as_array()
                .ok_or("Expected generated forms")?
                .len(),
            50
        );
        assert_eq!(result["selection_has_more"], true);
        assert_eq!(result["written"], false);
        assert_eq!(
            result["reason"],
            if index % 2 == 0 {
                "tracker_selection_required"
            } else {
                "provider_unavailable"
            }
        );
        let key = request
            .operation_key("api")
            .replacen("nl-operation:", "nl-context:", 1);
        let retained: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&key)
            .fetch_one(&pool)
            .await?;
        let repeated = database
            .tracker_text_fallback(&request, context("Europe/Bratislava"))
            .await?;
        assert_eq!(
            repeated["forms"]
                .as_array()
                .ok_or("Expected repeated forms")?
                .len(),
            50
        );
        let current: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(&key)
            .fetch_one(&pool)
            .await?;
        assert_eq!(current, retained);
        let pinned: Value = serde_json::from_str(&retained)?;
        assert_eq!(pinned["timezone"], "UTC");
        assert_eq!(
            StoredInstant::from_legacy_input(&pinned["message_at"]),
            Some(message_at)
        );
        let changed = TrackerTextRequest::from_value(
            &json!({"text":"Changed synthetic request","operation_id":request.operation_id}),
        )?;
        assert!(matches!(
            database
                .tracker_text_fallback(&changed, context("UTC"))
                .await,
            Err(TrackerTextError::Invalid)
        ));
    }
    let mut writer = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *writer)
        .await?;
    let request = TrackerTextRequest::from_value(
        &json!({"text":"unrelated","operation_id":"synthetic-concurrent-retry"}),
    )?;
    let (first, second) = tokio::join!(
        database.tracker_text_fallback(&request, context("UTC")),
        database.tracker_text_fallback(&request, context("UTC")),
    );
    assert_eq!(
        first?["forms"]
            .as_array()
            .ok_or("Expected concurrent forms")?
            .len(),
        50
    );
    assert_eq!(
        second?["forms"]
            .as_array()
            .ok_or("Expected concurrent retry forms")?
            .len(),
        50
    );
    writer.rollback().await?;
    let after: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(&pool)
            .await?;
    assert_eq!(after, before);
    println!(
        "Matched bounded forms and pinned retry context over {configured} trackers without changing facts or audits"
    );
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            println!("{line}");
        }
    }
    for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
        if let Ok(value) = fs::read_to_string(format!("/sys/fs/cgroup/{name}")) {
            println!("{name}\n{value}");
        }
    }
    Ok(())
}
