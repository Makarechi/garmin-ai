use std::{
    env,
    error::Error,
    io,
    str::FromStr,
    sync::atomic::{AtomicUsize, Ordering},
};

use ga_db::{
    Database,
    diary::{CreateSystemEvent, DiaryWriteError, UpdateSystemEvent},
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

    let previous = counts(&inspect).await?;
    let checks = AtomicUsize::new(0);
    assert!(matches!(
        db.create_system_event_unless_cancelled(request("synthetic-rust-event"), || {
            checks.fetch_add(1, Ordering::Relaxed) != 0
        })
        .await,
        Err(DiaryWriteError::Cancelled)
    ));
    assert_eq!(checks.load(Ordering::Relaxed), 2);
    assert_eq!(counts(&inspect).await?, previous);

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

    let edited = EventInput::from_json(AFTER_EDIT.as_bytes(), 4096, ValidationMode::NewReport)?;
    let correction = || UpdateSystemEvent {
        owner_id: person,
        authenticated_actor: "telegram:synthetic",
        event_id: entry.id,
        expected_revision: 1,
        report: &edited,
        operation_id: Some(operation),
    };
    let corrected = db.update_system_event(correction()).await?;
    assert_eq!(corrected.revision, 2);
    assert_eq!(corrected.snapshot["transport"], "telegram");
    assert!(matches!(
        db.update_system_event(correction()).await,
        Err(DiaryWriteError::RevisionChanged)
    ));
    let update_audit: (String, String) = sqlx::query_as(
        "SELECT before::text, after::text FROM audit_log WHERE event_id = $1 AND action = 'update'",
    )
    .bind(entry.id)
    .fetch_one(&inspect)
    .await?;
    assert_eq!(
        serde_json::from_str::<Value>(&update_audit.0)?,
        entry.snapshot
    );
    assert_eq!(
        serde_json::from_str::<Value>(&update_audit.1)?,
        corrected.snapshot
    );
    let generations: Vec<(i32, bool, bool)> = sqlx::query_as(
        "SELECT projection_version, valid, invalidated_at IS NOT NULL
         FROM metric_observations WHERE source_entry_id = $1 ORDER BY projection_version",
    )
    .bind(entry.id)
    .fetch_all(&inspect)
    .await?;
    assert_eq!(generations, [(1, false, true), (2, true, false)]);
    let retried = db
        .create_system_event(request("synthetic-rust-event"))
        .await?;
    assert_eq!(retried.id, entry.id);
    assert_eq!(retried.revision, 2);
    assert!(retried.reused);
    assert_eq!(retried.snapshot["payload"]["beverage"], "Synthetic edit");
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

    verify_episode_corrections(&db, &inspect, person).await?;

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

async fn verify_episode_corrections(
    db: &Database,
    inspect: &PgPool,
    person: Uuid,
) -> Result<(), Box<dyn Error>> {
    let start = "2026-10-25T10:00:00Z";
    let report =
        |payload: Value, end: Option<&str>, status: &str| -> Result<EventInput, Box<dyn Error>> {
            let input = json!({"start": start, "end": end, "status": status, "payload": payload});
            Ok(EventInput::from_json(
                &serde_json::to_vec(&input)?,
                4096,
                ValidationMode::NewReport,
            )?)
        };
    let migraine = json!({"type":"migraine","severity":4});
    let open = report(migraine.clone(), None, "confirmed")?;
    let create = |event| CreateSystemEvent {
        owner_id: person,
        authenticated_actor: "api",
        report: event,
        idempotency_key: None,
        operation_id: None,
        clock_uncertainty_seconds: None,
    };
    let episode = db.create_system_event(create(&open)).await?;
    let symptom = EventInput::from_json(
        &serde_json::to_vec(
            &json!({"start":"2026-10-25T10:45:00Z","payload":{"type":"symptom_observation","severity":3,"episode_id":episode.id.to_string()}}),
        )?,
        4096,
        ValidationMode::NewReport,
    )?;
    db.create_system_event(create(&symptom)).await?;
    let request = |event, revision| UpdateSystemEvent {
        owner_id: person,
        authenticated_actor: "api",
        event_id: episode.id,
        expected_revision: revision,
        report: event,
        operation_id: None,
    };

    let too_short = report(migraine.clone(), Some("2026-10-25T10:30:00Z"), "confirmed")?;
    let mut competing = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104619)")
        .execute(&mut *competing)
        .await?;
    assert!(matches!(
        db.update_system_event(request(&too_short, 1)).await,
        Err(DiaryWriteError::Busy)
    ));
    competing.rollback().await?;
    assert!(matches!(
        db.update_system_event(request(&too_short, 1)).await,
        Err(DiaryWriteError::SymptomBounds)
    ));
    let released: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(72104619)")
        .fetch_one(inspect)
        .await?;
    assert!(
        released,
        "the rejected correction still held the write lock"
    );
    let converted = report(
        json!({"type":"note","description":"Synthetic"}),
        None,
        "confirmed",
    )?;
    let conversion = db.update_system_event(request(&converted, 1)).await;
    assert!(
        matches!(conversion, Err(DiaryWriteError::LinkedObservations)),
        "unexpected linked episode conversion result: {:?}",
        conversion.as_ref().err()
    );
    let pending = report(migraine.clone(), None, "inferred")?;
    assert!(matches!(
        db.update_system_event(request(&pending, 1)).await,
        Err(DiaryWriteError::LinkedObservations)
    ));

    let question = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO pending_questions (id, kind, event_id, text, evidence, priority,
             earliest_send_at, expires_at, sent_at, status, dedup_key, attempts)
         VALUES ($1, 'migraine', $2, 'Synthetic migraine question', '{}'::jsonb, 1,
             now() - interval '3 days', now() - interval '1 day', now() - interval '2 days',
             'sent', 'diary:synthetic-migraine', 1)",
    )
    .bind(question)
    .bind(episode.id)
    .execute(inspect)
    .await?;
    let close = report(migraine, Some("2026-10-25T11:00:00Z"), "confirmed")?;
    assert_eq!(
        db.update_system_event(request(&close, 1)).await?.revision,
        2
    );
    let answered: (String, String) = sqlx::query_as(
        "SELECT status, evidence->>'answer_event_id' FROM pending_questions WHERE id = $1",
    )
    .bind(question)
    .fetch_one(inspect)
    .await?;
    assert_eq!(answered, ("answered".to_owned(), episode.id.to_string()));
    assert_eq!(db.update_system_event(request(&open, 2)).await?.revision, 3);
    let reopened: (String, bool, bool) = sqlx::query_as(
        "SELECT status, evidence ? 'answer_event_id', expires_at > now()
         FROM pending_questions WHERE id = $1",
    )
    .bind(question)
    .fetch_one(inspect)
    .await?;
    assert_eq!(reopened, ("sent".to_owned(), false, true));
    Ok(())
}
