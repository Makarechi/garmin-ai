use std::{
    error::Error,
    future::ready,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};

use serde_json::json;

use super::*;

struct Reader {
    tool: &'static str,
    current: AtomicBool,
    reads: AtomicUsize,
    revoke_after_read: bool,
    result: Value,
    tracker_version: Option<Uuid>,
}

impl AnalysisReadTools for Reader {
    fn categories(&self) -> ModelCategories {
        ModelCategories::HEALTH_AND_DIARY
    }

    async fn context_current(&self) -> bool {
        self.current.load(Ordering::SeqCst)
    }

    async fn read(&self, name: &str, arguments: &Map<String, Value>) -> Option<AnalysisToolRead> {
        assert_eq!(name, self.tool);
        if name == "personal_baseline" {
            assert_eq!(arguments["metric"], "sleep_score");
        }
        self.reads.fetch_add(1, Ordering::SeqCst);
        if self.revoke_after_read {
            self.current.store(false, Ordering::SeqCst);
        }
        Some(AnalysisToolRead {
            value: self.result.clone(),
            tracker_versions: self.tracker_version.into_iter().collect(),
            ..Default::default()
        })
    }
}

fn request<'a>(tools: &'a [OfferedReadTool<'a>], empty: &'a Value) -> AnalysisQuestion<'a> {
    AnalysisQuestion {
        now: "2026-11-17T14:00:00+01:00",
        timezone: "Europe/Bratislava",
        text: "Synthetic вопрос",
        locale: "en",
        conversation: empty,
        personal_goals: empty,
        replay_notice: None,
        tools,
    }
}

#[tokio::test]
async fn shares_the_interpretation_budget_and_executes_only_offered_reads()
-> Result<(), Box<dyn Error>> {
    let reader = Reader {
        tool: "personal_baseline",
        current: AtomicBool::new(true),
        reads: AtomicUsize::new(0),
        revoke_after_read: false,
        result: json!({"mean":78.5}),
        tracker_version: Some(Uuid::from_u128(42)),
    };
    let empty = json!({});
    let description = json!({"name":"personal_baseline"});
    let menu = [OfferedReadTool {
        name: "personal_baseline",
        description: &description,
    }];
    let mut prompts = Vec::new();
    let mut sent_requirements = Vec::new();
    let mut replies = [
        json!({"calls":[
            {"name":"delete_entry","arguments_json":"{}"},
            {"name":"personal_baseline","arguments_json":"[]"},
            {"name":"personal_baseline","arguments_json":r#"{"metric":"sleep_score","start":"2026-11-15","end":"2026-11-16"}"#},
        ]}),
        json!({"evidence_ids":[3],"numeric_claims":[{"evidence_id":3,"path":["mean"],"value":78.5}]}),
    ].into_iter();
    let now = Instant::now();
    let mut budget = AnalysisBudget::new(now);
    budget.record_model_call(
        AnalysisStage::Interpretation,
        "extract",
        "synthetic question",
        now,
    )?;
    let result = answer_with(
        request(&menu, &empty),
        &mut budget,
        &reader,
        |prompt, versions, _schemas, _evidence| {
            prompts.push(prompt);
            sent_requirements.push(versions);
            ready(replies.next().ok_or(AnalysisError::InvalidDocument))
        },
    )
    .await?;
    let AnalysisResult::Answer {
        numbers,
        used_evidence,
        tracker_versions,
        ..
    } = result
    else {
        return Err("Expected verified answer".into());
    };
    assert_eq!(numbers, ["Evidence 3: personal_baseline /mean: 78.5"]);
    assert_eq!(used_evidence.len(), 1);
    assert_eq!(tracker_versions, BTreeSet::from([Uuid::from_u128(42)]));
    assert_eq!(sent_requirements, [BTreeSet::new(), tracker_versions]);
    assert!(!prompts[1].contains(&Uuid::from_u128(42).to_string()));
    assert_eq!(reader.reads.load(Ordering::SeqCst), 1);
    assert_eq!(prompts.len(), 2);
    assert!(prompts[0].starts_with(r#"{"now": "2026-11-17T14:00:00+01:00", "timezone": "Europe/Bratislava", "question": "Synthetic вопрос""#));
    let first: Value = serde_json::from_str(&prompts[0])?;
    assert_eq!(first["remaining_tool_rounds"], 4);
    let second: Value = serde_json::from_str(&prompts[1])?;
    assert_eq!(
        second["evidence"][0]["result"]["error"],
        "Invalid tool arguments; inspect schema and retry"
    );
    assert_eq!(second["evidence"][2]["result"]["mean"], 78.5);
    assert_eq!(second["remaining_tool_calls"], 9);
    Ok(())
}

#[tokio::test]
async fn stops_before_a_second_model_call_after_context_revocation_or_oversized_evidence()
-> Result<(), Box<dyn Error>> {
    let empty = json!({});
    let description = json!({"name":"personal_baseline"});
    let menu = [OfferedReadTool {
        name: "personal_baseline",
        description: &description,
    }];
    for revoked in [true, false] {
        let reader = Reader {
            tool: "personal_baseline",
            current: AtomicBool::new(true),
            reads: AtomicUsize::new(0),
            revoke_after_read: revoked,
            tracker_version: None,
            result: if revoked {
                json!({"value":42})
            } else {
                json!({"value":"x".repeat(100_000)})
            },
        };
        let mut calls = 0;
        let result = answer_with(
            request(&menu, &empty),
            &mut AnalysisBudget::new(Instant::now()),
            &reader,
            |_, _, _, _| {
                calls += 1;
                ready(Ok(
                    json!({"calls":[{"name":"personal_baseline","arguments_json":r#"{"metric":"sleep_score","start":"2026-11-15","end":"2026-11-16"}"#}]}),
                ))
            },
        )
        .await;
        assert_eq!(calls, 1);
        assert_eq!(reader.reads.load(Ordering::SeqCst), 1);
        if revoked {
            assert!(matches!(result, Err(AnalysisError::EvidenceChanged)));
        } else {
            assert!(matches!(
                result,
                Err(AnalysisError::Budget(AnalysisBudgetError::Limit))
            ));
        }
    }
    Ok(())
}

#[tokio::test]
async fn carries_timeline_evidence_to_the_next_send_check() -> Result<(), Box<dyn Error>> {
    let reader = Reader {
        tool: TIMELINE,
        current: AtomicBool::new(true),
        reads: AtomicUsize::new(0),
        revoke_after_read: false,
        result: json!({"layers": [{"label": "synthetic timeline"}]}),
        tracker_version: None,
    };
    let empty = json!({});
    let description = json!({"name": TIMELINE});
    let menu = [OfferedReadTool {
        name: TIMELINE,
        description: &description,
    }];
    let mut checked = Vec::new();
    answer_with(
        request(&menu, &empty),
        &mut AnalysisBudget::new(Instant::now()),
        &reader,
        |_, versions, _, evidence| {
            assert!(versions.is_empty());
            assert!(evidence.definitions.is_empty());
            checked.push(evidence.timeline.len());
            ready(Ok(if evidence.timeline.is_empty() {
                json!({"calls": [{
                    "name": TIMELINE,
                    "arguments_json": r#"{"start":"2026-10-25T09:00:00Z","end":"2026-10-25T10:00:00Z"}"#,
                } ]})
            } else {
                json!({"answer": "Synthetic timeline result", "evidence_ids": [1]})
            }))
        },
    )
    .await?;
    assert_eq!(checked, [0, 1]);
    Ok(())
}
