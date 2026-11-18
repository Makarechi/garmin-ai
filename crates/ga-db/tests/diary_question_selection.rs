use std::{collections::BTreeMap, env, error::Error, fs};

use ga_db::{
    Database,
    proactive_control::{
        DiaryQuestionKind, NotificationExclusions, NotificationPolicy, ProactiveControlError,
    },
};
use ga_domain::timestamp::StoredInstant;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::postgres::PgConnectOptions;
use time::{OffsetDateTime, Time};
use uuid::Uuid;

#[derive(Deserialize)]
struct Fixture {
    owner: String,
    now: String,
    results: Vec<Value>,
    state: BTreeMap<String, Value>,
}

#[tokio::test]
#[ignore = "requires migration/check_diary_question_selection.py and its disposable database"]
async fn reserves_current_questions_without_overspending() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let suffix = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected test database URL")?;
    let parsed: PgConnectOptions = format!("postgresql://{suffix}").parse()?;
    if !parsed
        .get_database()
        .is_some_and(|name| name.ends_with("_test"))
    {
        return Err("Expected a disposable test database".into());
    }
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let pool = sqlx::PgPool::connect_with(parsed).await?;
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_QUESTION_SELECTION")?)?)?;
    let owner: Uuid = fixture.owner.parse()?;
    let now: StoredInstant = fixture.now.parse()?;
    let policy = NotificationPolicy {
        timezone: "UTC",
        default_enabled: true,
        daily_budget: Some(2),
        quiet_start: Time::from_hms(22, 0, 0)?,
        quiet_end: Time::from_hms(8, 0, 0)?,
        snoozed_until: None,
        destination: None,
        exclusions: NotificationExclusions::default(),
    };
    let midnight = "2026-10-25T02:00:00Z".parse()?;
    assert!(
        database
            .reserve_diary_question(owner, &policy, midnight)
            .await?
            .is_none()
    );
    assert!(
        database
            .reserve_diary_question(
                owner,
                &NotificationPolicy {
                    default_enabled: false,
                    ..policy
                },
                now
            )
            .await?
            .is_none()
    );

    let one_slot = NotificationPolicy {
        daily_budget: Some(1),
        ..policy
    };
    let (left, right) = tokio::join!(
        database.reserve_diary_question(owner, &one_slot, now),
        database.reserve_diary_question(owner, &one_slot, now),
    );
    let mut selected = None;
    for result in [left, right] {
        match result {
            Ok(Some(question)) => assert!(selected.replace(question).is_none()),
            Ok(None) | Err(ProactiveControlError::Busy) => {}
            Err(error) => return Err(error.into()),
        }
    }
    let first = selected.ok_or("Neither caller reserved the eligible question")?;
    assert_eq!(first.kind, DiaryQuestionKind::Migraine);
    assert_eq!(first.attempt, 1);
    assert_eq!(first.id.to_string(), fixture.results[0]["id"]);
    assert_eq!(first.text, fixture.results[0]["text"]);
    assert!(
        database
            .reserve_diary_question(owner, &one_slot, now)
            .await?
            .is_none()
    );

    let second = database
        .reserve_diary_question(owner, &policy, now)
        .await?
        .ok_or("Missing second question")?;
    assert_eq!(second.kind, DiaryQuestionKind::Caffeine);
    for (question, expected) in [first, second].into_iter().zip(&fixture.results) {
        let kind = match question.kind {
            DiaryQuestionKind::Migraine => "migraine",
            DiaryQuestionKind::Caffeine => "caffeine",
            DiaryQuestionKind::Context => "context",
        };
        assert_eq!(
            &json!({
                "id": question.id.to_string(),
                "event_id": question.event_id.map(|event| event.to_string()),
                "kind": kind,
                "text": question.text,
                "attempt": question.attempt,
                "reserved_at": question.reserved_at.to_string(),
            }),
            expected
        );
    }
    assert!(
        database
            .reserve_diary_question(owner, &policy, now)
            .await?
            .is_none()
    );
    assert_eq!(fixture.results[2], Value::Null);
    let rows: Vec<(Uuid, String, Option<OffsetDateTime>, OffsetDateTime)> = sqlx::query_as(
        "SELECT id,jsonb_build_object('status',status,'attempt',attempts,'text',text)::text,
         sent_at,expires_at FROM pending_questions",
    )
    .fetch_all(&pool)
    .await?;
    let mut state = BTreeMap::new();
    for (id, document, sent, expires) in rows {
        let mut row: Value = serde_json::from_str(&document)?;
        row["sent"] = json!(
            sent.map(StoredInstant::try_from)
                .transpose()?
                .map(|at| at.to_string())
        );
        row["expires"] = json!(StoredInstant::try_from(expires)?.to_string());
        state.insert(id.to_string(), row);
    }
    assert_eq!(state, fixture.state);
    println!("Verified current evidence, quiet hours, simultaneous reservations and legacy state");
    Ok(())
}
