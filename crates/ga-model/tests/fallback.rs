use std::{
    collections::{BTreeMap, VecDeque},
    env,
    error::Error,
    fs,
    time::Duration,
};

use ga_domain::{
    model_gate::{ModelGatePause, ModelGateReason, ModelGateState},
    model_response::ModelResponseError,
    timestamp::StoredInstant,
};
use ga_model::{
    ModelTransportError, ModelUnavailability,
    fallback::{FallbackError, FallbackRuntime, GateUpdate, ModelFallback},
};
use serde_json::{Value, json};

struct Fixture<'a> {
    at: StoredInstant,
    responses: VecDeque<&'a str>,
    attempted: Vec<Option<String>>,
    writes: Vec<Value>,
    job: bool,
    can_checkpoint: bool,
}

impl FallbackRuntime for Fixture<'_> {
    type Output = &'static str;

    fn now(&self) -> StoredInstant {
        self.at
    }

    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn attempt(&mut self, model: Option<&str>) -> Result<Self::Output, FallbackError> {
        self.attempted.push(model.map(str::to_owned));
        match self.responses.pop_front() {
            Some("ok") => Ok("synthetic result"),
            Some("auth") => Err(unavailable(ModelUnavailability::Authorization, 1800)),
            Some("quota") => Err(unavailable(ModelUnavailability::RateLimited, 120)),
            Some("upstream") => Err(unavailable(ModelUnavailability::Upstream, 60)),
            Some("earlier_equal") => Err(unavailable(ModelUnavailability::MissingModel, 120)),
            Some("invalid") => Err(FallbackError::Provider(ModelTransportError::Response(
                ModelResponseError::InvalidOutput,
            ))),
            Some("incomplete") => Err(FallbackError::Provider(ModelTransportError::Response(
                ModelResponseError::Incomplete,
            ))),
            Some("request") => Err(FallbackError::Provider(
                ModelTransportError::RequestRejected,
            )),
            Some("deadline") => Err(FallbackError::Deadline),
            Some("denied") => Err(FallbackError::ConsentRequired),
            _ => panic!("Fallback made an unexpected extra attempt"),
        }
    }

    async fn record_gate(&mut self, update: GateUpdate<'_>) {
        let cooldowns: BTreeMap<_, _> = update
            .model_cooldowns
            .iter()
            .map(|(name, until)| (name, until.to_string()))
            .collect();
        self.writes.push(json!([
            "gate",
            update.reason.map_or("ready", gate_reason),
            update.blocked_until.map(|value| value.to_string()),
            update.scheduler_pause_until.map(|value| value.to_string()),
            cooldowns,
        ]));
    }

    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn checkpoint(&mut self, invalid: &[Option<&str>]) -> bool {
        if self.job {
            self.writes.push(json!(["checkpoint", invalid]));
        }
        self.job && self.can_checkpoint
    }

    async fn clear_checkpoint(&mut self) {
        self.writes.push(json!(["clear"]));
    }
}

#[tokio::test]
#[ignore = "uses the pinned Python fallback sequence fixture"]
async fn compares_attempt_order_and_saved_fallback_effects() -> Result<(), Box<dyn Error>> {
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_FALLBACK")?)?)?;
    let at: StoredInstant = "2026-03-12T12:00:00Z".parse()?;
    let at_offset = |seconds| {
        StoredInstant::from_unix_microseconds(at.unix_microseconds() + seconds * 1_000_000)
    };
    for case in cases {
        let blocked = case["blocked"]
            .as_i64()
            .map(|delay| {
                at_offset(delay).map(|until| ModelGatePause {
                    until,
                    reason: ModelGateReason::Quota,
                })
            })
            .transpose()?;
        let model_cooldowns = case["cooldowns"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(name, delay)| {
                Ok::<_, Box<dyn Error>>((
                    name.clone(),
                    at_offset(delay.as_i64().ok_or("Unrecognized cooldown")?)?,
                ))
            })
            .collect::<Result<_, _>>()?;
        let prior = ModelGateState {
            blocked,
            model_cooldowns,
            scheduler_pause_until: None,
        };
        let models = names(&case["models"])?;
        let denied = names(&case["excluded"])?;
        let mut runtime = Fixture {
            at,
            responses: names(&case["steps"])?.into_iter().flatten().collect(),
            attempted: Vec::new(),
            writes: Vec::new(),
            job: case["job"].as_bool().unwrap_or(true),
            can_checkpoint: case["checkpoint"].as_bool().unwrap_or(true),
        };
        let observed = ModelFallback {
            models: &models,
            excluded: &denied,
            prior,
        }
        .run(&mut runtime)
        .await;
        assert!(
            runtime.responses.is_empty(),
            "{} missing attempts",
            case["name"]
        );
        assert_eq!(
            json!(runtime.attempted),
            case["attempted"],
            "{} attempts",
            case["name"]
        );
        assert_eq!(
            json!(runtime.writes),
            case["writes"],
            "{} gate writes",
            case["name"]
        );
        assert_eq!(
            outcome(observed),
            case["outcome"],
            "{} result",
            case["name"]
        );
    }
    Ok(())
}

fn names(value: &Value) -> Result<Vec<Option<&str>>, Box<dyn Error>> {
    Ok(value
        .as_array()
        .ok_or("Expected labels")?
        .iter()
        .map(Value::as_str)
        .collect())
}

fn unavailable(reason: ModelUnavailability, seconds: u64) -> FallbackError {
    FallbackError::Provider(ModelTransportError::Unavailable {
        reason,
        retry_after: Duration::from_secs(seconds),
    })
}

fn outcome(result: Result<&'static str, FallbackError>) -> Value {
    match result {
        Ok(result) => json!(["ok", result]),
        Err(FallbackError::Cooldown {
            reason,
            retry_after,
        }) => json!(["cooldown", gate_reason(reason), retry_after.as_secs()]),
        Err(FallbackError::ConsentRequired) => json!(["denied"]),
        Err(FallbackError::Deadline) => json!(["deadline"]),
        Err(FallbackError::Provider(ModelTransportError::Unavailable {
            reason,
            retry_after,
        })) => json!([
            match reason {
                ModelUnavailability::Authorization => "auth",
                ModelUnavailability::RateLimited => "quota",
                ModelUnavailability::MissingModel => "missing",
                ModelUnavailability::Upstream => "upstream",
            },
            retry_after.as_secs()
        ]),
        Err(FallbackError::Provider(ModelTransportError::Response(
            ModelResponseError::InvalidOutput,
        ))) => json!(["invalid"]),
        Err(FallbackError::Provider(ModelTransportError::Response(
            ModelResponseError::Incomplete,
        ))) => json!(["incomplete"]),
        Err(FallbackError::Provider(ModelTransportError::RequestRejected)) => json!(["request"]),
        Err(error) => json!([format!("{error:?}")]),
    }
}

fn gate_reason(reason: ModelGateReason) -> &'static str {
    match reason {
        ModelGateReason::Auth => "auth",
        ModelGateReason::Busy => "busy",
        ModelGateReason::Model => "model",
        ModelGateReason::ModelCooldown => "model_cooldown",
        ModelGateReason::Quota => "quota",
        ModelGateReason::Unavailable => "unavailable",
    }
}
