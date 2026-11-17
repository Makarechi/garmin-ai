use std::{env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    diary::{CreateSystemEvent, DiaryWriteError},
};
use ga_domain::{
    event_input::{EventInput, InvalidEvent, ValidationMode},
    event_replay::ReplayConflict,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

const REPORT: &str = r#"{"start":"2026-10-24T23:30:00+00:00","payload":{"type":"caffeine","beverage":"Synthetic original","caffeine_mg_estimate":95}}"#;
const AFTER_EDIT: &str = r#"{"start":"2026-10-25T01:30:00+02:00","payload":{"type":"caffeine","beverage":"Synthetic edit","caffeine_mg_estimate":95}}"#;

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB with Python's system metric fixture"]
async fn creates_diary_event_audit_and_metric_together_and_replays_the_original()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, expected);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let person: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let input = EventInput::from_json(REPORT.as_bytes(), 4096, ValidationMode::NewReport)?;
    let operation = Uuid::new_v4();
    let request = |key: &'static str| CreateSystemEvent {
        owner_id: person,
        authenticated_actor: "api",
        report: &input,
        idempotency_key: Some(key),
        operation_id: Some(operation),
        clock_uncertainty_seconds: None,
    };
    let mut unknown = request("synthetic-incorrect-owner");
    unknown.owner_id = Uuid::new_v4();
    assert!(matches!(
        db.create_system_event(unknown).await,
        Err(DiaryWriteError::UnknownOwner)
    ));
    let historical = EventInput::from_json(
        br#"{"start":"2026-10-25T10:00:00Z","source":"inferred","status":"inferred","payload":{"type":"wellbeing_observation","energy":5}}"#,
        4096, ValidationMode::AuditedSnapshot,
    )?;
    let mut restored = request("synthetic-audited-observation");
    restored.report = &historical;
    assert!(matches!(
        db.create_system_event(restored).await,
        Err(DiaryWriteError::Input(InvalidEvent::ExplicitReportRequired))
    ));

    let legacy = db
        .create_system_event(request("synthetic-python-event"))
        .await?;
    assert!(legacy.reused);
    assert_eq!(legacy.revision, 1);

    let entry = db
        .create_system_event(request("synthetic-rust-event"))
        .await?;
    assert!(!entry.reused);
    assert_eq!(entry.revision, 1);
    assert_eq!(entry.snapshot["transport"], "api");
    assert_eq!(entry.snapshot["producer"], "owner");
    assert_eq!(entry.snapshot["start"], "2026-10-24T23:30:00+00:00");
    assert_eq!(entry.snapshot["recorded_at"], entry.snapshot["ingested_at"]);
    assert!(entry.snapshot["end"].is_null());

    let audit: (String, Uuid, String) = sqlx::query_as(
        "SELECT actor, operation_id, after::text FROM audit_log WHERE event_id = $1 AND action = 'create'",
    )
    .bind(entry.id).fetch_one(&inspect).await?;
    assert_eq!(audit.0, "api");
    assert_eq!(audit.1, operation);
    assert_eq!(serde_json::from_str::<Value>(&audit.2)?, entry.snapshot);
    let observations: Vec<(String, f64, String, String, Option<i32>, bool)> = sqlx::query_as(
        "SELECT metric, value, unit, source_calendar_date::text, projection_version,
                recorded_at = ingested_at
         FROM metric_observations WHERE source_entry_id = $1",
    )
    .bind(entry.id)
    .fetch_all(&inspect)
    .await?;
    assert_eq!(
        observations,
        [(
            "user.synthetic-caffeine-dose".to_owned(),
            95.0,
            "mg".to_owned(),
            "2026-10-25".to_owned(),
            Some(1),
            true
        ),]
    );

    sqlx::query(
        "UPDATE events SET revision = 2, payload = payload || '{\"beverage\":\"Synthetic edit\"}'::jsonb WHERE id = $1",
    )
    .bind(entry.id).execute(&inspect).await?;
    let retried = db
        .create_system_event(request("synthetic-rust-event"))
        .await?;
    assert_eq!(retried.id, entry.id);
    assert_eq!(retried.revision, 2);
    assert!(retried.reused);
    assert_eq!(retried.snapshot["payload"]["beverage"], "Synthetic edit");
    let edited = EventInput::from_json(AFTER_EDIT.as_bytes(), 4096, ValidationMode::NewReport)?;
    let mut conflict = request("synthetic-rust-event");
    conflict.report = &edited;
    assert!(matches!(
        db.create_system_event(conflict).await,
        Err(DiaryWriteError::Replay(ReplayConflict::InputChanged))
    ));

    let before: (i64, i64, i64) = counts(&inspect).await?;
    sqlx::query("ALTER TABLE metric_observations ADD CONSTRAINT synthetic_projection_gate CHECK (false) NOT VALID")
        .execute(&inspect).await?;
    assert!(matches!(
        db.create_system_event(request("synthetic-failed-projection"))
            .await,
        Err(DiaryWriteError::Database)
    ));
    assert_eq!(before, counts(&inspect).await?);
    sqlx::query("ALTER TABLE metric_observations DROP CONSTRAINT synthetic_projection_gate")
        .execute(&inspect)
        .await?;

    let medication = EventInput::from_json(
        br#"{"start":"2026-10-25T10:00:00Z","payload":{"type":"medication"}}"#,
        4096,
        ValidationMode::NewReport,
    )?;
    let mut medication_request = request("synthetic-unknown-medication");
    medication_request.report = &medication;
    let logged = db.create_system_event(medication_request).await?;
    assert!(logged.snapshot["payload"]["name"].is_null());
    assert!(logged.snapshot["payload"]["dose"].is_null());
    assert!(logged.snapshot["payload"]["unit"].is_null());

    let absent_relation = json!({
        "start": "2026-10-25T10:00:00Z",
        "payload": {"type": "symptom_observation", "episode_id": Uuid::new_v4().to_string(), "severity": 4},
    });
    let invalid = EventInput::from_json(
        &serde_json::to_vec(&absent_relation)?,
        4096,
        ValidationMode::NewReport,
    )?;
    let mut invalid_request = request("synthetic-absent-related-episode");
    invalid_request.report = &invalid;
    assert!(matches!(
        db.create_system_event(invalid_request).await,
        Err(DiaryWriteError::InvalidRelation)
    ));

    sqlx::query(
        "INSERT INTO module_configs (id, owner_id, pack_key, tracking_enabled, collection_enabled,
            reminders_enabled, visible, llm_enabled, settings, revision)
         VALUES ($1, $2, 'caffeine', false, false, false, false, false, '{}'::jsonb, 1)
         ON CONFLICT (owner_id, pack_key) DO UPDATE SET tracking_enabled = false",
    )
    .bind(Uuid::new_v4())
    .bind(person)
    .execute(&inspect)
    .await?;
    assert!(matches!(
        db.create_system_event(request("synthetic-disabled-tracking"))
            .await,
        Err(DiaryWriteError::TrackingDisabled)
    ));
    assert!(
        db.create_system_event(request("synthetic-rust-event"))
            .await?
            .reused
    );
    Ok(())
}

async fn counts(db: &PgPool) -> Result<(i64, i64, i64), sqlx::Error> {
    sqlx::query_as(
        "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM audit_log),
            (SELECT count(*) FROM metric_observations)",
    )
    .fetch_one(db)
    .await
}
