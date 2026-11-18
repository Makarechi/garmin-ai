use std::time::{Duration, SystemTime, UNIX_EPOCH};

use ga_db::{
    model_gate::{
        ModelGateError, ModelGateOutcomeReason, ModelGateSession, ModelResume, RecordModelGate,
    },
    share_policy::{
        DestinationKind, ModelChannelSharing, ShareDestination, SharePolicyError, ShareRequirement,
    },
};
use ga_domain::{
    model_consent::{ModelCategories, ModelOperation},
    model_gate::ModelGateReason,
    model_request::ModelRequestError,
    model_response::StructuredModelOutput,
    sha256_hex,
    timestamp::StoredInstant,
};
use ga_model::{
    ModelTransportError,
    fallback::{FallbackError, FallbackRuntime, GateUpdate, ModelFallback},
};
use tokio::time::Instant;

use super::{
    ActiveModelJob, CurrentModelPolicy, GatewayError, ModelGateway, ModelInput, PreparedRequest,
    Sender,
};

const BUSY_RETRY: Duration = Duration::from_secs(1);

pub(super) struct ModelRequestScope<'a> {
    pub requirements: &'a [ShareRequirement<'a>],
    pub channel: Option<ModelChannelSharing<'a>>,
    pub job: Option<ActiveModelJob>,
}

impl ModelGateway<'_> {
    pub(super) async fn execute_with<'policy, P, S>(
        &self,
        input: ModelInput<'_>,
        policy: &P,
        requirements: &[ShareRequirement<'_>],
        job: Option<ActiveModelJob>,
        sender: &S,
    ) -> Result<StructuredModelOutput, GatewayError>
    where
        P: Fn() -> CurrentModelPolicy<'policy> + Sync,
        S: Sender,
    {
        self.execute_with_context(
            input,
            policy,
            ModelRequestScope {
                requirements,
                channel: None,
                job,
            },
            sender,
        )
        .await
    }

    pub(super) async fn execute_with_context<'policy, P, S>(
        &self,
        input: ModelInput<'_>,
        policy: &P,
        scope: ModelRequestScope<'_>,
        sender: &S,
    ) -> Result<StructuredModelOutput, GatewayError>
    where
        P: Fn() -> CurrentModelPolicy<'policy> + Sync,
        S: Sender,
    {
        let ModelRequestScope {
            requirements,
            channel,
            job,
        } = scope;
        let clock = RequestClock::new()?;
        let deadline = clock.started + self.selected.deadline;
        let operation = input.operation();
        let approved = self
            .authorize(policy(), operation, clock.now())
            .map_err(GatewayError::Consent)?;
        if !(1..=9).contains(&approved.len()) {
            return Err(GatewayError::Configuration);
        }
        let categories = operation.categories();
        let onboarding =
            tokio::time::timeout_at(deadline, self.database.model_onboarding_allows(categories))
                .await
                .map_err(|_| GatewayError::Attempt(FallbackError::Deadline))?
                .map_err(GatewayError::State)?;
        if !onboarding {
            return Err(GatewayError::Attempt(FallbackError::ConsentRequired));
        }

        let prepared = self.prepare(input)?;
        let models: Vec<_> = match prepared {
            PreparedRequest::Gemini(_) => approved.iter().copied().map(Some).collect(),
            PreparedRequest::Structured(_) => vec![None],
        };
        let fingerprint = job
            .map(|_| fingerprint(&prepared, &approved, categories))
            .transpose()
            .map_err(GatewayError::Request)?;
        let resume = job
            .zip(fingerprint.as_deref())
            .map(|(job, fingerprint)| ModelResume {
                job_id: job.id,
                lease_token: job.lease_token,
                request_fingerprint: fingerprint,
                models: &models,
            });
        let (state, excluded, prior) = tokio::time::timeout_at(deadline, async {
            let mut state = self
                .database
                .lock_model_gate(&self.identity)
                .await
                .map_err(|error| match error {
                    ModelGateError::Busy => GatewayError::Attempt(busy()),
                    error => GatewayError::State(error),
                })?;
            let excluded = match resume {
                Some(resume) => state
                    .read_resume(resume)
                    .await
                    .map_err(GatewayError::State)?,
                None => Vec::new(),
            };
            let prior = state.read(clock.now()).await.map_err(GatewayError::State)?;
            Ok::<_, GatewayError>((state, excluded, prior))
        })
        .await
        .map_err(|_| GatewayError::Attempt(FallbackError::Deadline))??;
        let excluded: Vec<_> = excluded.iter().map(Option::as_deref).collect();
        let mut runtime = Runtime {
            gateway: self,
            prepared,
            policy,
            operation,
            requirements,
            channel,
            deadline,
            clock,
            state,
            resume,
            sender,
        };
        ModelFallback {
            models: &models,
            excluded: &excluded,
            prior,
        }
        .run(&mut runtime)
        .await
        .map_err(GatewayError::Attempt)
    }
}

fn fingerprint(
    request: &PreparedRequest,
    models: &[&str],
    categories: ModelCategories,
) -> Result<String, ModelRequestError> {
    let mut categories: Vec<_> = categories.labels().collect();
    categories.sort_unstable();
    match request {
        PreparedRequest::Gemini(request) => request.resume_fingerprint(models, &categories),
        PreparedRequest::Structured(_) => {
            // Python's plugin gate receives a closed request, hence its empty kwargs.
            let categories = categories
                .iter()
                .map(|label| format!("'{label}'"))
                .collect::<Vec<_>>()
                .join(", ");
            Ok(sha256_hex(
                format!("([None], [{categories}], {{}})").as_bytes(),
            ))
        }
    }
}

#[derive(Clone, Copy)]
struct RequestClock {
    wall: StoredInstant,
    started: Instant,
}

impl RequestClock {
    fn new() -> Result<Self, GatewayError> {
        let micros = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .ok()
            .and_then(|elapsed| i64::try_from(elapsed.as_micros()).ok())
            .ok_or(GatewayError::Clock)?;
        Ok(Self {
            wall: StoredInstant::from_unix_microseconds(micros).map_err(|_| GatewayError::Clock)?,
            started: Instant::now(),
        })
    }

    fn now(self) -> StoredInstant {
        let elapsed = i64::try_from(self.started.elapsed().as_micros()).unwrap_or(i64::MAX);
        StoredInstant::from_unix_microseconds(self.wall.unix_microseconds().saturating_add(elapsed))
            .unwrap_or(self.wall)
    }
}

struct Runtime<'a, 'config, P, S> {
    gateway: &'a ModelGateway<'config>,
    prepared: PreparedRequest,
    policy: &'a P,
    operation: ModelOperation,
    requirements: &'a [ShareRequirement<'a>],
    channel: Option<ModelChannelSharing<'a>>,
    deadline: Instant,
    clock: RequestClock,
    state: ModelGateSession<'a>,
    resume: Option<ModelResume<'a>>,
    sender: &'a S,
}

impl<'policy, P, S> FallbackRuntime for Runtime<'_, '_, P, S>
where
    P: Fn() -> CurrentModelPolicy<'policy> + Sync,
    S: Sender,
{
    type Output = StructuredModelOutput;

    fn now(&self) -> StoredInstant {
        self.clock.now()
    }

    async fn attempt(&mut self, model: Option<&str>) -> Result<Self::Output, FallbackError> {
        if Instant::now() >= self.deadline {
            return Err(FallbackError::Deadline);
        }
        let candidate = model.unwrap_or(self.gateway.selected.primary_model);
        if let PreparedRequest::Gemini(request) = &mut self.prepared {
            request
                .select_model(candidate)
                .map_err(|_| FallbackError::Provider(ModelTransportError::InvalidConfiguration))?;
        }
        let database = &self.gateway.database;
        let binding = self.gateway.binding();
        let destination = ShareDestination::new(DestinationKind::Model, binding.instance_id)
            .map_err(|_| FallbackError::StateUnavailable)?;
        tokio::time::timeout_at(self.deadline, async {
            if let Some(resume) = self.resume {
                self.state
                    .read_resume(resume)
                    .await
                    .map_err(|_| FallbackError::StateUnavailable)?;
            }
            database
                .with_model_delivery(
                    destination,
                    self.operation.categories(),
                    self.requirements,
                    self.channel,
                    || async {
                        let allowed = self
                            .gateway
                            .authorize((self.policy)(), self.operation, self.now())
                            .map_err(|_| FallbackError::ConsentRequired)?;
                        if !allowed.contains(&candidate) {
                            return Err(FallbackError::ConsentRequired);
                        }
                        let remaining = self.deadline.saturating_duration_since(Instant::now());
                        if remaining.is_zero() {
                            return Err(FallbackError::Deadline);
                        }
                        self.sender
                            .send(&self.prepared, remaining)
                            .await
                            .map_err(FallbackError::Provider)
                    },
                )
                .await
                .map_err(|error| match error {
                    SharePolicyError::Busy => busy(),
                    _ => FallbackError::StateUnavailable,
                })?
                .ok_or(FallbackError::ConsentRequired)?
        })
        .await
        .map_err(|_| FallbackError::Deadline)?
    }

    async fn record_gate(&mut self, update: GateUpdate<'_>) {
        let reason = match update.reason {
            None => ModelGateOutcomeReason::Ready,
            Some(ModelGateReason::Quota) => ModelGateOutcomeReason::Quota,
            Some(ModelGateReason::Auth) => ModelGateOutcomeReason::Auth,
            Some(ModelGateReason::Model) => ModelGateOutcomeReason::Model,
            Some(ModelGateReason::ModelCooldown) => ModelGateOutcomeReason::ModelCooldown,
            Some(ModelGateReason::Busy | ModelGateReason::Unavailable) => {
                ModelGateOutcomeReason::Unavailable
            }
        };
        let _ = self
            .state
            .record(RecordModelGate {
                now: self.clock.now(),
                reason,
                blocked_until: update.blocked_until,
                scheduler_pause_until: update.scheduler_pause_until,
                model_cooldowns: update.model_cooldowns,
                notify_quota: self.gateway.notify_quota,
            })
            .await;
    }

    async fn checkpoint(&mut self, invalid: &[Option<&str>]) -> bool {
        match self.resume {
            Some(resume) => self.state.record_resume(resume, invalid).await.is_ok(),
            None => false,
        }
    }

    async fn clear_checkpoint(&mut self) {
        if let Some(resume) = self.resume {
            let _ = self.state.clear_resume(resume).await;
        }
    }
}

fn busy() -> FallbackError {
    FallbackError::Cooldown {
        reason: ModelGateReason::Busy,
        retry_after: BUSY_RETRY,
    }
}
