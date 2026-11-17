use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    time::Duration,
};

use ga_domain::{
    model_gate::{ModelGateReason, ModelGateState},
    model_response::ModelResponseError,
    timestamp::StoredInstant,
};

use crate::{ModelTransportError, ModelUnavailability};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FallbackError {
    ConsentRequired,
    Cooldown {
        reason: ModelGateReason,
        retry_after: Duration,
    },
    Deadline,
    NoEligibleModel,
    Provider(ModelTransportError),
}

pub struct GateUpdate<'a> {
    /// `None` records a ready provider while individual candidates can remain cooled.
    pub reason: Option<ModelGateReason>,
    pub blocked_until: Option<StoredInstant>,
    pub scheduler_pause_until: Option<StoredInstant>,
    pub model_cooldowns: &'a BTreeMap<String, StoredInstant>,
}

pub trait FallbackRuntime {
    type Output;

    fn now(&self) -> StoredInstant;

    /// Verify the current permissions and remaining request budget before sending once.
    fn attempt(
        &mut self,
        model: Option<&str>,
    ) -> impl Future<Output = Result<Self::Output, FallbackError>> + Send;

    /// A write failure after an attempt must not cause the attempt to be repeated.
    fn record_gate(&mut self, update: GateUpdate<'_>) -> impl Future<Output = ()> + Send;

    /// Return true only after persisting this checkpoint for the active operation.
    fn checkpoint(&mut self, invalid: &[Option<&str>]) -> impl Future<Output = bool> + Send;

    fn clear_checkpoint(&mut self) -> impl Future<Output = ()> + Send;
}

pub struct ModelFallback<'a> {
    pub models: &'a [Option<&'a str>],
    pub excluded: &'a [Option<&'a str>],
    pub prior: ModelGateState,
}

impl ModelFallback<'_> {
    pub async fn run<R: FallbackRuntime>(
        self,
        runtime: &mut R,
    ) -> Result<R::Output, FallbackError> {
        let ModelFallback {
            models,
            excluded,
            prior,
        } = self;
        let now = runtime.now();
        if let Some(blocked) = prior.blocked.filter(|pause| pause.until > now) {
            return Err(cooldown(blocked.reason, blocked.until, now));
        }
        if models.is_empty() {
            return Err(FallbackError::NoEligibleModel);
        }
        let mut denied: BTreeSet<_> = excluded.iter().copied().collect();
        let mut model_cooldowns = prior.model_cooldowns;
        model_cooldowns.retain(|_, until| *until > now);
        let mut cooldown_order: Vec<_> = model_cooldowns.keys().cloned().collect();
        // Existing Python records are read in PostgreSQL JSONB key order.
        cooldown_order.sort_by(|a, b| a.len().cmp(&b.len()).then_with(|| a.cmp(b)));
        let (cooling, pending): (Vec<_>, Vec<_>) = models
            .iter()
            .copied()
            .filter(|model| !denied.contains(model))
            .partition(|model| model.is_some_and(|name| model_cooldowns.contains_key(name)));
        let initially_cooling_until = cooling
            .iter()
            .filter_map(|model| model.and_then(|name| model_cooldowns.get(name)))
            .copied()
            .min();
        if pending.is_empty() {
            if let Some(until) = initially_cooling_until {
                record(
                    runtime,
                    &model_cooldowns,
                    Some(ModelGateReason::ModelCooldown),
                    Some(until),
                    None,
                )
                .await;
                return Err(cooldown(
                    ModelGateReason::ModelCooldown,
                    until,
                    runtime.now(),
                ));
            }
            runtime.clear_checkpoint().await;
            record(runtime, &model_cooldowns, None, None, None).await;
            return Err(invalid_output());
        }
        let mut failures = Vec::new();
        let mut first_invalid = None;
        for model in pending {
            let result = runtime.attempt(model).await;
            let error = match result {
                Ok(output) => {
                    runtime.clear_checkpoint().await;
                    record(runtime, &model_cooldowns, None, None, None).await;
                    return Ok(output);
                }
                Err(error) => error,
            };
            match error {
                FallbackError::ConsentRequired | FallbackError::Cooldown { .. } => {
                    return Err(error);
                }
                FallbackError::Deadline => {
                    if first_invalid.is_some() {
                        let checkpoint: Vec<_> = models
                            .iter()
                            .copied()
                            .filter(|model| denied.contains(model))
                            .collect();
                        if runtime.checkpoint(&checkpoint).await {
                            return Err(FallbackError::Cooldown {
                                reason: ModelGateReason::ModelCooldown,
                                retry_after: Duration::from_secs(1),
                            });
                        }
                    }
                    if models.len() == 1 {
                        record_global(
                            runtime,
                            &model_cooldowns,
                            ModelGateReason::Unavailable,
                            Duration::from_secs(1),
                        )
                        .await;
                    }
                    return Err(error);
                }
                FallbackError::Provider(ModelTransportError::Unavailable {
                    reason,
                    retry_after,
                }) => {
                    if reason == ModelUnavailability::Authorization
                        && failures.is_empty()
                        && initially_cooling_until.is_none()
                        && model == models[0]
                    {
                        record_global(
                            runtime,
                            &model_cooldowns,
                            ModelGateReason::Auth,
                            retry_after,
                        )
                        .await;
                        return Err(error);
                    }
                    if let Some(name) = model {
                        if !model_cooldowns.contains_key(name) {
                            cooldown_order.push(name.to_owned());
                        }
                        model_cooldowns.insert(name.to_owned(), after(runtime.now(), retry_after));
                    }
                    failures.push((model, error));
                    record(runtime, &model_cooldowns, None, None, None).await;
                }
                FallbackError::Provider(ModelTransportError::Response(
                    ModelResponseError::InvalidOutput
                    | ModelResponseError::InvalidEnvelope
                    | ModelResponseError::Incomplete
                    | ModelResponseError::TooLarge,
                )) => {
                    denied.insert(model);
                    first_invalid.get_or_insert(error);
                }
                _ => {
                    if first_invalid.is_some() || !excluded.is_empty() {
                        runtime.clear_checkpoint().await;
                        record(runtime, &model_cooldowns, None, None, None).await;
                        return Err(first_invalid.unwrap_or_else(invalid_output));
                    }
                    let failed_until = failures
                        .iter()
                        .filter_map(|(name, _)| name.and_then(|name| model_cooldowns.get(name)))
                        .copied()
                        .min();
                    if let Some(until) = failed_until.or(initially_cooling_until) {
                        record(runtime, &model_cooldowns, None, None, Some(until)).await;
                        return Err(cooldown(
                            ModelGateReason::ModelCooldown,
                            until,
                            runtime.now(),
                        ));
                    }
                    return Err(error);
                }
            }
        }
        if let Some(invalid) = first_invalid {
            let all_invalid = models.iter().all(|model| denied.contains(model));
            if all_invalid || initially_cooling_until.is_none() {
                if all_invalid {
                    runtime.clear_checkpoint().await;
                }
                record(runtime, &model_cooldowns, None, None, None).await;
                return Err(invalid);
            }
        }
        if models.len() > 1
            && let Some(until) = models
                .iter()
                .filter_map(|model| model.and_then(|name| model_cooldowns.get(name)))
                .copied()
                .min()
        {
            let all_cooling = models
                .iter()
                .all(|model| model.is_some_and(|name| model_cooldowns.contains_key(name)));
            let earliest_name = cooldown_order
                .iter()
                .find(|name| model_cooldowns.get(*name) == Some(&until));
            let earliest_failure = failures.iter().find_map(|(name, error)| {
                (*name == earliest_name.map(String::as_str)).then_some(*error)
            });
            if all_cooling || earliest_failure.is_none() {
                let reason = if !all_cooling {
                    ModelGateReason::ModelCooldown
                } else if failures.iter().any(|(_, error)| quota(*error)) {
                    ModelGateReason::Quota
                } else {
                    ModelGateReason::Unavailable
                };
                record(runtime, &model_cooldowns, Some(reason), Some(until), None).await;
            }
            if let Some(FallbackError::Provider(ModelTransportError::Unavailable {
                reason, ..
            })) = earliest_failure
            {
                return Err(FallbackError::Provider(ModelTransportError::Unavailable {
                    reason,
                    retry_after: remaining(until, runtime.now()),
                }));
            }
            return Err(cooldown(
                ModelGateReason::ModelCooldown,
                until,
                runtime.now(),
            ));
        }
        let failure = failures
            .iter()
            .find(|(_, error)| quota(*error))
            .or(failures.first())
            .map(|(_, error)| *error)
            .or(first_invalid)
            .unwrap_or(FallbackError::NoEligibleModel);
        if models.len() == 1
            && let FallbackError::Provider(ModelTransportError::Unavailable {
                reason,
                retry_after,
            }) = failure
        {
            record_global(
                runtime,
                &model_cooldowns,
                reason_for_unavailability(reason),
                retry_after,
            )
            .await;
        }
        Err(failure)
    }
}

async fn record<R: FallbackRuntime>(
    runtime: &mut R,
    model_cooldowns: &BTreeMap<String, StoredInstant>,
    reason: Option<ModelGateReason>,
    blocked_until: Option<StoredInstant>,
    scheduler_pause_until: Option<StoredInstant>,
) {
    runtime
        .record_gate(GateUpdate {
            reason,
            blocked_until,
            scheduler_pause_until,
            model_cooldowns,
        })
        .await;
}

async fn record_global<R: FallbackRuntime>(
    runtime: &mut R,
    model_cooldowns: &BTreeMap<String, StoredInstant>,
    reason: ModelGateReason,
    duration: Duration,
) {
    let until = after(runtime.now(), duration);
    record(runtime, model_cooldowns, Some(reason), Some(until), None).await;
}

fn invalid_output() -> FallbackError {
    FallbackError::Provider(ModelTransportError::Response(
        ModelResponseError::InvalidOutput,
    ))
}

fn quota(error: FallbackError) -> bool {
    matches!(
        error,
        FallbackError::Provider(ModelTransportError::Unavailable {
            reason: ModelUnavailability::RateLimited,
            ..
        })
    )
}

fn after(now: StoredInstant, duration: Duration) -> StoredInstant {
    // Keep the persisted deadline within Python's representable UTC time range.
    const MAX_MICROS: i64 = 253_402_300_799_999_999;
    let delay = i64::try_from(duration.as_micros()).unwrap_or(i64::MAX);
    StoredInstant::from_unix_microseconds(
        now.unix_microseconds()
            .saturating_add(delay)
            .min(MAX_MICROS),
    )
    .unwrap_or(now)
}

fn remaining(until: StoredInstant, now: StoredInstant) -> Duration {
    let micros = until
        .unix_microseconds()
        .saturating_sub(now.unix_microseconds());
    Duration::from_secs(
        u64::try_from(micros)
            .unwrap_or_default()
            .div_ceil(1_000_000)
            .max(1),
    )
}

fn cooldown(reason: ModelGateReason, until: StoredInstant, now: StoredInstant) -> FallbackError {
    FallbackError::Cooldown {
        reason,
        retry_after: remaining(until, now),
    }
}

fn reason_for_unavailability(reason: ModelUnavailability) -> ModelGateReason {
    match reason {
        ModelUnavailability::Authorization => ModelGateReason::Auth,
        ModelUnavailability::MissingModel => ModelGateReason::Model,
        ModelUnavailability::RateLimited => ModelGateReason::Quota,
        ModelUnavailability::Upstream => ModelGateReason::Unavailable,
    }
}
