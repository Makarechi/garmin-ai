use std::{env, fs};

use serde_json::json;

use super::*;

#[derive(Deserialize)]
struct Fixture {
    name: String,
    request: Request,
    expected: Value,
    instruction: String,
}

#[derive(Deserialize)]
struct Request {
    now: String,
    timezone: String,
    text: String,
    context: ModelDiaryContext,
}

#[test]
#[ignore = "requires migration/check_diary_interpretation_prompt.py from the pinned runtime"]
fn matches_pinned_diary_prompt_context() -> Result<(), Box<dyn Error>> {
    let cases: Vec<Fixture> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_PROMPT")?)?)?;
    assert_eq!(cases.len(), 2);
    for fixture in cases {
        let request = fixture.request;
        let prepared = PreparedPrompt::new(DiaryInterpretationRequest {
            now: request.now.parse()?,
            timezone: &request.timezone,
            source: EventSource::TelegramText,
            text: &request.text,
            context: request.context,
        })?;
        let actual: Value = serde_json::from_str(&prepared.json)?;
        assert_eq!(actual, fixture.expected, "{}", fixture.name);
        assert_eq!(EXTRACT.trim(), fixture.instruction);
        assert!(!prepared.json.contains("synthetic-private-source-text"));
        if fixture.name == "oversized_context" {
            assert!(prepared.events.is_empty());
        } else {
            assert_eq!(prepared.events.len(), 20);
            assert_eq!(actual["context"]["recent_events"][0]["kind"], "migraine");
            assert_eq!(actual["context"]["recent_events"][1]["kind"], "illness");
        }
    }
    Ok(())
}

#[test]
fn checks_buried_event_links_without_restricting_local_drafts() -> Result<(), Box<dyn Error>> {
    let known = Uuid::from_u128(42).to_string();
    let unknown = Uuid::from_u128(43).to_string();
    let now = "2026-10-25T01:31:00+00:00".parse()?;
    let prepare = |large: bool| {
        PreparedPrompt::new(DiaryInterpretationRequest {
            now,
            timezone: "UTC",
            source: EventSource::TelegramText,
            text: "Synthetic reply",
            context: ModelDiaryContext {
                recent_events: vec![
                    json!({"id":known,"kind":"migraine","status":"confirmed","end":null}),
                ],
                pending_clarification: large.then(|| json!({"prompt":"x".repeat(30_000)})),
                ..ModelDiaryContext::default()
            },
        })
    };
    for (kind, field) in [
        ("medication", "reason_event_id"),
        ("symptom_observation", "episode_id"),
    ] {
        let response = |id: &str| {
            let mut payload = if kind == "medication" {
                json!({"type":kind,"name":"synthetic"})
            } else {
                json!({"type":kind,"severity":4})
            };
            payload[field] = json!(id);
            json!({"intent":"log","confidence":1.0,
                "events":[{"start":"2026-10-25T01:30:00+00:00","payload":payload}]})
        };
        assert!(prepare(false)?.accept(response(&known)).is_ok());
        assert!(matches!(
            prepare(false)?.accept(response(&unknown)),
            Err(DiaryInterpretationError::UnofferedTarget)
        ));
        assert!(matches!(
            prepare(true)?.accept(response(&known)),
            Err(DiaryInterpretationError::UnofferedTarget)
        ));
    }
    let draft = prepare(true)?.accept(json!({
        "intent":"log","confidence":1.0,"events":[
            {"start":"2026-10-25T01:25:00+00:00","payload":{"type":"migraine"}},
            {"start":"2026-10-25T01:30:00+00:00","payload":{"type":"medication","name":"synthetic"}}
        ],"draft_links":[{"child_index":1,"parent_index":0}],
    }))?;
    assert_eq!(draft.draft_links().len(), 1);
    Ok(())
}

#[test]
fn checks_offered_references_and_oversized_messages() -> Result<(), Box<dyn Error>> {
    let known = Uuid::from_u128(42);
    let unknown = Uuid::from_u128(43).to_string();
    let now = "2026-10-25T01:31:00+00:00".parse()?;
    let write = |text: &str, context| {
        PreparedPrompt::new(DiaryInterpretationRequest {
            now,
            timezone: "UTC",
            source: EventSource::TelegramText,
            text,
            context,
        })
    };
    assert!(matches!(
        write(&"🧪".repeat(16_001), ModelDiaryContext::default()),
        Err(DiaryInterpretationError::RequiresSafetyScreen)
    ));
    let offered = || ModelDiaryContext {
        recent_questions: vec![json!({"id":known.to_string(),"text":"Synthetic"})],
        ..ModelDiaryContext::default()
    };
    let valid = write("Synthetic reply", offered())?.accept(json!({
        "intent":"acknowledge","confidence":1.0,"target_question_id":known.to_string(),
    }))?;
    assert_eq!(valid.target_question_id(), Some(known));
    for field in ["target_event_id", "target_question_id"] {
        let mut invalid = json!({"intent":"acknowledge","confidence":1.0});
        invalid[field] = json!(unknown);
        assert!(matches!(
            write("Synthetic reply", offered())?.accept(invalid),
            Err(DiaryInterpretationError::UnofferedTarget)
        ));
    }
    let invalid_dependency = write("Synthetic reply", offered())?.accept(json!({
        "intent":"log","confidence":1.0,
        "events":[{"start":"2026-10-25T01:30:00+00:00","payload":{"type":"migraine"}}],
        "question_context_ids":[unknown],
    }));
    assert!(matches!(
        invalid_dependency,
        Err(DiaryInterpretationError::UnofferedTarget)
    ));
    Ok(())
}
