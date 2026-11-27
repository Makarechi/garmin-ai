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
    result: Result<Option<Value>, AnalysisReadError>,
    tracker_version: Option<Uuid>,
}

impl AnalysisReadTools for Reader {
    fn categories(&self) -> ModelCategories {
        ModelCategories::HEALTH_AND_DIARY
    }

    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn context_current(&self) -> bool {
        self.current.load(Ordering::SeqCst)
    }

    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn read(
        &self,
        name: &str,
        arguments: &Map<String, Value>,
    ) -> Result<Option<AnalysisToolRead>, AnalysisReadError> {
        assert_eq!(name, self.tool);
        if name == "personal_baseline" {
            assert_eq!(arguments["metric"], "sleep_score");
        }
        self.reads.fetch_add(1, Ordering::SeqCst);
        if self.revoke_after_read {
            self.current.store(false, Ordering::SeqCst);
        }
        Ok(self.result.clone()?.map(|value| AnalysisToolRead {
            value,
            tracker_versions: self.tracker_version.into_iter().collect(),
            ..Default::default()
        }))
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

#[test]
fn renders_each_selected_sleep_review_once_per_night() {
    let review = |days: &[&str], history_start: &str, history_end: &str| {
        json!({"tool": SLEEP_REVIEW, "result": {
            "rows": days.iter().map(|day| json!({"day":day,"available":true,
                "values":{"duration_minutes":420}})).collect::<Vec<_>>(),
            "baseline":{"start":history_start,"end":history_end,
                "metrics":{"duration_minutes":{"nights":28,"median":410}}}
        }})
    };
    let response = format_analysis_answer(
        "ru",
        None,
        None,
        Vec::new(),
        Vec::new(),
        &[
            review(&["2026-11-14"], "2026-10-17", "2026-11-13"),
            review(&["2026-11-14", "2026-11-15"], "2026-10-17", "2026-11-13"),
            review(&["2026-11-17"], "2026-10-20", "2026-11-16"),
        ],
    );
    for day in ["2026-11-14", "2026-11-15", "2026-11-17"] {
        let night = format!("{day} · Сон 7 ч 0 мин");
        assert_eq!(response.lines().filter(|line| *line == night).count(), 1);
    }
    assert_eq!(response.matches("Медианная длительность сна").count(), 2);
}

struct SleepQualityReader;

#[expect(clippy::unused_async_trait_impl, reason = "This fixture has no I/O")]
impl AnalysisReadTools for SleepQualityReader {
    fn categories(&self) -> ModelCategories {
        ModelCategories::HEALTH_AND_DIARY
    }

    async fn context_current(&self) -> bool {
        true
    }

    async fn quality_report(&self) -> Option<(Value, bool)> {
        Some((
            json!({"checked_at":"2026-11-17T14:00:00+01:00","channels":{"body_battery":{
                "newest_observed_at":"2026-11-17T12:00:00+01:00","observation_lag_seconds":7200,
                "quality_reason":"stale_observation","refresh_mode":"frequent"
            }}}),
            true,
        ))
    }

    async fn read(
        &self,
        name: &str,
        _arguments: &Map<String, Value>,
    ) -> Result<Option<AnalysisToolRead>, AnalysisReadError> {
        let value = match name {
            SLEEP_REVIEW => {
                json!({"algorithm_version":"sleep-review-v1","timezone":"Europe/Bratislava"})
            }
            "personal_baseline" => json!({"mean":70}),
            _ => return Ok(None),
        };
        Ok(Some(AnalysisToolRead {
            value,
            ..Default::default()
        }))
    }
}

#[tokio::test]
async fn keeps_freshness_for_mixed_recovery_questions() -> Result<(), Box<dyn Error>> {
    let empty = json!({});
    let menu = [
        OfferedReadTool {
            name: SLEEP_REVIEW,
            description: &empty,
        },
        OfferedReadTool {
            name: "personal_baseline",
            description: &empty,
        },
    ];
    for mixed in [false, true] {
        let mut calls = vec![json!({"name":SLEEP_REVIEW,
            "arguments_json":r#"{"start":"2026-11-17","end":"2026-11-17"}"#})];
        if mixed {
            calls.push(json!({"name":"personal_baseline",
                "arguments_json":r#"{"metric":"body_battery","start":"2026-11-17","end":"2026-11-17"}"#}));
        }
        let mut replies = [
            json!({"calls":calls}),
            json!({"answer":"Synthetic recovery result",
            "evidence_ids":[1], "include_current_state_freshness":mixed}),
        ]
        .into_iter();
        let result = answer_with(
            request(&menu, &empty),
            &mut AnalysisBudget::new(Instant::now()),
            &SleepQualityReader,
            |_, _, _, _| ready(replies.next().ok_or(AnalysisError::InvalidDocument)),
        )
        .await?;
        let AnalysisResult::Answer { freshness, .. } = result else {
            return Err("Expected an analysis answer".into());
        };
        assert_eq!(
            freshness.is_some_and(|text| text.contains("Body Battery")),
            mixed
        );
    }
    Ok(())
}

#[tokio::test]
async fn shares_the_interpretation_budget_and_executes_only_offered_reads()
-> Result<(), Box<dyn Error>> {
    let reader = Reader {
        tool: "personal_baseline",
        current: AtomicBool::new(true),
        reads: AtomicUsize::new(0),
        revoke_after_read: false,
        result: Ok(Some(json!({"mean":78.5}))),
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
            result: Ok(Some(if revoked {
                json!({"value":42})
            } else {
                json!({"value":"x".repeat(100_000)})
            })),
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
        result: Ok(Some(json!({"layers": [{"label": "synthetic timeline"}]}))),
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

#[tokio::test]
async fn stops_before_more_reads_or_model_calls_when_storage_is_unavailable() {
    let empty = json!({});
    let description = json!({"name": "personal_baseline"});
    let menu = [OfferedReadTool {
        name: "personal_baseline",
        description: &description,
    }];
    for error in [
        AnalysisReadError::Unavailable,
        AnalysisReadError::ReplayPending,
    ] {
        let reader = Reader {
            tool: "personal_baseline",
            current: AtomicBool::new(true),
            reads: AtomicUsize::new(0),
            revoke_after_read: false,
            result: Err(error),
            tracker_version: None,
        };
        let mut calls = 0;
        let result = answer_with(
            request(&menu, &empty),
            &mut AnalysisBudget::new(Instant::now()),
            &reader,
            |_, _, _, _| {
                calls += 1;
                let read = json!({
                    "name": "personal_baseline",
                    "arguments_json": r#"{"metric":"sleep_score","start":"2026-11-15","end":"2026-11-16"}"#,
                });
                ready(Ok(json!({"calls": [read.clone(), read]})))
            },
        )
        .await;
        assert!(matches!(result, Err(AnalysisError::Read(actual)) if actual == error));
        assert_eq!(calls, 1);
        assert_eq!(reader.reads.load(Ordering::SeqCst), 1);
        assert!(catalog_text("en", error.notice_key()).is_some());
        assert!(catalog_text("ru", error.notice_key()).is_some());
    }
}

#[tokio::test]
async fn keeps_argument_rejection_available_for_model_correction() -> Result<(), Box<dyn Error>> {
    let reader = Reader {
        tool: SLEEP_ANALYSIS,
        current: AtomicBool::new(true),
        reads: AtomicUsize::new(0),
        revoke_after_read: false,
        result: Ok(None),
        tracker_version: None,
    };
    let empty = json!({});
    let description = json!({"name": SLEEP_ANALYSIS});
    let menu = [OfferedReadTool {
        name: SLEEP_ANALYSIS,
        description: &description,
    }];
    let mut prompts = Vec::new();
    let result = answer_with(
        request(&menu, &empty),
        &mut AnalysisBudget::new(Instant::now()),
        &reader,
        |prompt, _, _, _| {
            prompts.push(prompt);
            ready(Ok(if prompts.len() == 1 {
                json!({"calls": [{
                    "name": SLEEP_ANALYSIS,
                    "arguments_json": r#"{"start":"invalid","end":"2026-11-16"}"#,
                } ]})
            } else {
                json!({})
            }))
        },
    )
    .await?;
    assert!(matches!(result, AnalysisResult::NoAnswer { .. }));
    assert_eq!(reader.reads.load(Ordering::SeqCst), 1);
    assert_eq!(prompts.len(), 2);
    let correction: Value = serde_json::from_str(&prompts[1])?;
    assert_eq!(
        correction["evidence"][0]["result"]["error"],
        "Invalid tool arguments; inspect schema and retry"
    );
    Ok(())
}
