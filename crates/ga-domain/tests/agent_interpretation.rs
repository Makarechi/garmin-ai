use std::{env, error::Error, fs};

use ga_domain::agent_interpretation::{AgentIntent, AgentInterpretation, InterpretationError};
use serde_json::{Value, json};
use uuid::Uuid;

#[test]
fn rejects_a_mutation_without_events_or_a_target() {
    let empty = json!({"intent":"log","confidence":0.8});
    assert!(matches!(
        AgentInterpretation::from_value(&empty),
        Err(InterpretationError::MissingEvents)
    ));
    let no_target = json!({
        "intent":"update","confidence":0.8,
        "events":[{"start":"2026-11-17T12:00:00Z","payload":{"type":"migraine","severity":6}}]
    });
    assert!(matches!(
        AgentInterpretation::from_value(&no_target),
        Err(InterpretationError::TargetRequired)
    ));
}

#[test]
fn capability_answers_cannot_carry_record_actions() -> Result<(), InterpretationError> {
    let answer = json!({"intent":"question","confidence":0.97,
        "response_language":"ja","capability_answer":" Garminのデータと日記を確認できます。 "});
    assert_eq!(
        AgentInterpretation::from_value(&answer)?.capability_answer(),
        Some("Garminのデータと日記を確認できます。")
    );
    for (field, value) in [
        ("intent", json!("safety")),
        ("capability_answer", json!(" ")),
        ("capability_answer", json!("a".repeat(901))),
        ("clarification", json!("What record should I change?")),
        (
            "target_event_id",
            json!("00000000-0000-0000-0000-000000000004"),
        ),
        (
            "question_context_ids",
            json!(["00000000-0000-0000-0000-000000000005"]),
        ),
        ("changed_fields", json!(["payload.description"])),
    ] {
        let mut invalid = answer.clone();
        invalid[field] = value;
        assert!(matches!(
            AgentInterpretation::from_value(&invalid),
            Err(InterpretationError::InvalidResponse)
        ));
    }
    Ok(())
}

#[test]
#[ignore = "matches synthetic interpretations from pinned Python"]
fn matches_structured_commands_and_cross_field_validation() -> Result<(), Box<dyn Error>> {
    let fixture: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_AGENT_INTERPRETATION")?)?)?;
    assert!(!fixture.is_empty());
    for case in fixture {
        let result = AgentInterpretation::from_value(&case["input"]);
        if case["result"].is_null() {
            assert!(result.is_err(), "{}", case["name"]);
            continue;
        }
        let result = result?;
        let events: Vec<_> = result
            .events()
            .iter()
            .map(|event| {
                json!({
                    "kind":event.payload().kind(), "source":event.source().label(),
                    "status":event.status().label(), "timezone":event.timezone(),
                })
            })
            .collect();
        let links: Vec<_> = result
            .draft_links()
            .iter()
            .map(|link| {
                json!({
                    "child_index":link.child_index,"parent_index":link.parent_index,
                })
            })
            .collect();
        let offered: Vec<_> = result
            .question_context_ids()
            .iter()
            .map(Uuid::to_string)
            .collect();
        let label = match result.intent() {
            AgentIntent::Log => "log",
            AgentIntent::Update => "update",
            AgentIntent::Close => "close",
            AgentIntent::Undo => "undo",
            AgentIntent::Question => "question",
            AgentIntent::Clarify => "clarify",
            AgentIntent::Safety => "safety",
            AgentIntent::Acknowledge => "acknowledge",
        };
        assert_eq!(
            json!({
                "intent":label,"events":events,"draft_links":links,
                "target_event_id":result.target_event_id().map(|id|id.to_string()),
                "target_question_id":result.target_question_id().map(|id|id.to_string()),
                "question_context_ids":offered,"clarification":result.clarification(),
                "confidence":result.confidence(),"response_language":result.response_language(),
                "changed_fields":result.changed_fields(),
            }),
            case["result"],
            "{}",
            case["name"]
        );
    }
    Ok(())
}
