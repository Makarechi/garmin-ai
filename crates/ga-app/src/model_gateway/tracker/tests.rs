use std::{
    env,
    error::Error,
    str::FromStr,
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

use ga_db::{Database, diary::actions::TrackerTextContext};
use ga_domain::{
    access::{Scope, Scopes},
    model_response::StructuredModelOutput,
    timestamp::StoredInstant,
};
use ga_model::ModelTransportError;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

use super::*;
use crate::model_gateway::{PreparedRequest, RuntimeModelSettings};

struct SyntheticTrackerModel {
    calls: AtomicUsize,
}
impl Sender for SyntheticTrackerModel {
    async fn send(
        &self,
        request: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        self.calls.fetch_add(1, Ordering::SeqCst);
        let PreparedRequest::Gemini(request) = request else {
            return Err(ModelTransportError::InvalidConfiguration);
        };
        assert_eq!(request.body()["system_instruction"], INSTRUCTION.trim());
        let prompt: Value = serde_json::from_str(
            request.body()["input"]
                .as_str()
                .ok_or(ModelTransportError::InvalidConfiguration)?,
        )
        .map_err(|_| ModelTransportError::InvalidConfiguration)?;
        assert_eq!(
            prompt
                .as_object()
                .ok_or(ModelTransportError::InvalidConfiguration)?
                .len(),
            5
        );
        let candidate = &prompt["candidate_trackers"][0];
        assert!(candidate.get("events").is_none());
        assert!(candidate.get("original_text").is_none());
        let text = prompt["text"]
            .as_str()
            .ok_or(ModelTransportError::InvalidConfiguration)?;
        if text.ends_with("purple") {
            tokio::time::sleep(Duration::from_millis(1600)).await;
        }
        let target = text
            .split_whitespace()
            .last()
            .ok_or(ModelTransportError::InvalidConfiguration)?;
        let value = target.trim_matches('"');
        let begin = text
            .rfind(value)
            .ok_or(ModelTransportError::InvalidConfiguration)?;
        let start = text[..begin].chars().count();
        let fields = json!([{"field_id":candidate["fields"][0]["field_id"],"value":value,"evidence":{"start":start,"end":start+value.chars().count(),"quote":value},"unit":null,"unit_evidence":null}]);
        let mut output = json!({"schema_version":"tracker.nl.v1","intent":"create_entry","definition_version_id":candidate["definition_version_id"],"event_id":null,"start":prompt["now"],"end":null,"start_evidence":{"start":0,"end":text.chars().count(),"quote":text},"end_evidence":null,"fields":fields,"tracker_draft":null,"clarification":null,"confidence":0.99});
        if !prompt["selected_event"].is_null() {
            output["intent"] = json!("update_entry");
            output["event_id"] = prompt["selected_event"]["id"].clone();
            output["start"] = Value::Null;
            output["start_evidence"] = Value::Null;
        }
        if text.contains("low-confidence") {
            output["confidence"] = json!(0.5);
        }
        if text.contains("unoffered") {
            output["definition_version_id"] = json!(Uuid::from_u128(42).to_string());
        }
        let body = serde_json::to_vec(&json!({"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":output.to_string()}]}]})).map_err(|_| ModelTransportError::InvalidConfiguration)?;
        request
            .decode_response(&body)
            .map_err(ModelTransportError::Response)
    }
}

#[tokio::test]
#[ignore = "uses three source-created synthetic trackers on an exact disposable TimescaleDB target"]
async fn applies_only_evidenced_tracker_text_and_replays_atomic_receipts()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let options =
        PgConnectOptions::from_str(&url.replace("postgresql+psycopg://", "postgresql://"))?
            .disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test") && name.len() <= 63)
        .ok_or("Expected isolated test target")?
        .to_owned();
    let pool = PgPool::connect_with(options).await?;
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT current_database()")
            .fetch_one(&pool)
            .await?,
        expected
    );
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid isolated connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    let (version,sensitive):(Uuid,Uuid) = sqlx::query_as("SELECT (SELECT v.id FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version WHERE d.key='user.synthetic_nl_001'),(SELECT v.id FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id AND v.version=d.current_version WHERE d.key='user.synthetic_nl_000')").fetch_one(&pool).await?;
    let before: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(&pool)
            .await?;
    let now: StoredInstant = "2026-09-20T20:00:00+00:00".parse()?;
    let settings = RuntimeModelSettings::resolve(|name| Ok(match name {
        "GA_LLM_ENABLED"=>Some("true".into()),"GA_GEMINI_API_KEY"=>Some("synthetic-tracker-key".into()),"GA_GEMINI_MODEL"=>Some("synthetic-tracker-model".into()),
        "GA_LLM_CONSENT"=>Some(json!({"provider":"gemini","provider_instance_id":"model:gemini:primary","model":"synthetic-tracker-model","categories":["diary"],"granted_at":"2026-09-20T19:00:00+00:00","policy_revision":1}).to_string()),_=>None,
    }),|_| Ok(None))?.ok_or("Expected synthetic model")?;
    let configured = settings.configured();
    let gateway = configured.gateway(database.clone(), false, now)?;
    let model = SyntheticTrackerModel {
        calls: AtomicUsize::new(0),
    };
    let scopes: Scopes = [
        Scope::ReadDiary,
        Scope::WriteDiary,
        Scope::ManageDefinitions,
    ]
    .into_iter()
    .collect();
    let context = |scopes| TrackerTextContext {
        owner,
        scopes,
        actor: "api",
        message_at: now,
        configured_timezone: "UTC",
        configured_locale: "en",
    };
    let request = |text: &str, operation: &str, selected: Option<Uuid>| {
        ga_domain::tracker_nl::TrackerTextRequest::from_value(
            &json!({"text":text,"operation_id":operation,"selected_definition_version_id":version.to_string(),"selected_event_id":selected.map(|id| id.to_string())}),
        )
    };
    let create = request("Synthetic now blue", "synthetic-model-create", None)?;
    let saved = gateway
        .tracker_text_with(&create, context(scopes), &|| configured.current(), &model)
        .await?;
    assert_eq!(saved["written"], true);
    assert_eq!(saved["revision"], 1);
    assert_eq!(saved["definition_version_id"], version.to_string());
    assert_eq!(model.calls.load(Ordering::SeqCst), 1);
    let repeated = gateway
        .tracker_text_with(&create, context(scopes), &|| configured.current(), &model)
        .await?;
    assert_eq!(repeated, saved);
    assert_eq!(model.calls.load(Ordering::SeqCst), 1);
    let manage: Scopes = [Scope::ManageDefinitions].into_iter().collect();
    assert!(matches!(
        gateway
            .tracker_text_with(&create, context(manage), &|| configured.current(), &model)
            .await,
        Err(TrackerTextError::Forbidden)
    ));
    assert_eq!(model.calls.load(Ordering::SeqCst), 1);
    let id: Uuid = saved["event_id"]
        .as_str()
        .ok_or("Expected saved entry")?
        .parse()?;
    let (text, evidence): (String, String) =
        sqlx::query_as("SELECT original_text,evidence_refs::text FROM events WHERE id=$1")
            .bind(id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(text, create.text);
    let evidence: Value = serde_json::from_str(&evidence)?;
    assert!(
        evidence
            .as_array()
            .ok_or("Expected field provenance")?
            .iter()
            .any(|reference| reference["field_id"] == "user.synthetic_nl_001.value")
    );
    let update = request("Synthetic now green", "synthetic-model-update", Some(id))?;
    let updated = gateway
        .tracker_text_with(&update, context(scopes), &|| configured.current(), &model)
        .await?;
    assert_eq!(updated["revision"], 2);
    assert_eq!(updated["event_id"], saved["event_id"]);
    assert_eq!(
        gateway
            .tracker_text_with(&update, context(scopes), &|| configured.current(), &model)
            .await?,
        updated
    );
    assert_eq!(model.calls.load(Ordering::SeqCst), 2);
    let current: (String, String, i32) =
        sqlx::query_as("SELECT original_text,payload->>'value',revision FROM events WHERE id=$1")
            .bind(id)
            .fetch_one(&pool)
            .await?;
    assert_eq!(current, (update.text.clone(), "green".to_owned(), 2));
    for (index, text) in [
        "Synthetic now not blue",
        "Synthetic now will blue",
        "Synthetic now \"blue\"",
        "Synthetic now low-confidence blue",
    ]
    .into_iter()
    .enumerate()
    {
        let input = request(text, &format!("synthetic-clarify-{index}"), None)?;
        let result = gateway
            .tracker_text_with(&input, context(scopes), &|| configured.current(), &model)
            .await?;
        assert_eq!(result["written"], false, "{result}");
        assert_eq!(result["intent"], "clarify");
    }
    let input = request("Synthetic now unoffered blue", "synthetic-unoffered", None)?;
    assert!(matches!(
        gateway
            .tracker_text_with(&input, context(scopes), &|| configured.current(), &model)
            .await,
        Err(TrackerTextError::Invalid)
    ));
    let calls = model.calls.load(Ordering::SeqCst);
    let private = ga_domain::tracker_nl::TrackerTextRequest::from_value(
        &json!({"text":"Synthetic now blue","operation_id":"synthetic-sensitive","selected_definition_version_id":sensitive.to_string()}),
    )?;
    let result = gateway
        .tracker_text_with(&private, context(scopes), &|| configured.current(), &model)
        .await?;
    assert_eq!(result["reason"], "sensitive_tracker_consent_required");
    assert_eq!(model.calls.load(Ordering::SeqCst), calls);
    let concurrent = request("Synthetic now purple", "synthetic-concurrent-model", None)?;
    let destination = ga_db::share_policy::ShareDestination::new(
        ga_db::share_policy::DestinationKind::Model,
        "model:gemini:primary",
    )?;
    let prepared = database
        .prepare_tracker_text(&concurrent, context(scopes), Some(destination))
        .await?;
    let TrackerTextPreparation::Model(session) = prepared else {
        return Err("Expected pending model session".into());
    };
    let first = async {
        tokio::time::sleep(Duration::from_millis(50)).await;
        let prompt = session.prompt()?;
        let requirements = session.sharing_requirements()?;
        let response = gateway
            .execute_with(
                ModelInput::Structured {
                    contract: ApprovedModelSchema::TrackerExtraction,
                    instruction: INSTRUCTION.trim(),
                    prompt: &prompt,
                    categories: ModelCategories::DIARY,
                },
                &|| configured.current(),
                &requirements,
                None,
                &model,
            )
            .await?;
        let result = session
            .apply(TrackerExtraction::from_value(&response.value)?)
            .await?;
        Ok::<_, Box<dyn Error>>(result)
    };
    let policy = || configured.current();
    let retry = gateway.tracker_text_with(&concurrent, context(scopes), &policy, &model);
    let web_chat = async {
        tokio::time::sleep(Duration::from_millis(200)).await;
        let mut snapshot = database.web_chat_messages().await?;
        let mut body = Vec::new();
        while let Some(chunk) = snapshot.next_json().await? {
            body.extend(chunk);
        }
        assert!(serde_json::from_slice::<Value>(&body)?["messages"].is_array());
        Ok::<_, Box<dyn Error>>(())
    };
    let (first, retry, web_chat) = tokio::join!(first, retry, web_chat);
    web_chat?;
    assert_eq!(first?, retry?);
    println!(
        "A 1.6-second provider request overlapped a successful web-chat read and a matching retry with one saved receipt"
    );
    assert_eq!(model.calls.load(Ordering::SeqCst), calls + 1);
    let after: (i64, i64) =
        sqlx::query_as("SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM audit_log)")
            .fetch_one(&pool)
            .await?;
    assert_eq!(after, (before.0 + 2, before.1 + 3));
    println!(
        "Verified source-evidenced model creation, correction, retained provenance, scope checks, atomic retries and refused uncertain/unoffered/sensitive writes in {} synthetic SDK calls",
        model.calls.load(Ordering::SeqCst)
    );
    if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
        for line in status
            .lines()
            .filter(|line| line.starts_with("VmHWM:") || line.starts_with("VmRSS:"))
        {
            println!("{line}");
        }
    }
    for name in ["memory.peak", "memory.swap.peak", "memory.events"] {
        if let Ok(value) = std::fs::read_to_string(format!("/sys/fs/cgroup/{name}")) {
            println!("{name}\n{value}");
        }
    }
    Ok(())
}
