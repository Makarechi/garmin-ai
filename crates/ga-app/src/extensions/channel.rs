pub(super) mod delivery;

use std::{error::Error, path::Path};

use ga_db::{
    delivery::DeliveryState,
    outbox_claims::{ReplyObservation, ReplySendOutcome},
};
use ga_domain::{channel::OutboundIntent, timestamp::StoredInstant};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::watch;
use uuid::Uuid;

use super::{Extension, ExtensionError, MAX_REPLY, PROBE_TIMEOUT, run_until_shutdown};

const TEXT: &str = "Fictional contract probe";
const NOW: &str = "2026-01-02T00:00:00+00:00";

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Capabilities {
    text: bool,
    actions: bool,
    voice: bool,
    edit: bool,
    reply: bool,
    attachments: bool,
    initiatives: bool,
    max_text_length: usize,
}

impl Default for Capabilities {
    fn default() -> Self {
        Self {
            text: true,
            actions: false,
            voice: false,
            edit: false,
            reply: false,
            attachments: false,
            initiatives: false,
            max_text_length: 4096,
        }
    }
}

impl Capabilities {
    fn unverified(&self) -> Vec<&'static str> {
        [
            ("actions", self.actions),
            ("attachments", self.attachments),
            ("voice", self.voice),
            ("edit", self.edit),
            ("reply", self.reply),
            ("initiatives", self.initiatives),
        ]
        .into_iter()
        .filter_map(|(name, enabled)| enabled.then_some(name))
        .collect()
    }
}

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Policy {
    allow_delivery: bool,
    allow_initiative: bool,
    reason: Option<String>,
    retry_after: Option<String>,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            allow_delivery: true,
            allow_initiative: true,
            reason: None,
            retry_after: None,
        }
    }
}

#[derive(Clone, Copy, Deserialize, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum Observed {
    Queued,
    ProviderAccepted,
    Delivered,
    Read,
    Failed,
    Uncertain,
    Cancelled,
    Expired,
}

impl Observed {
    fn rank(self) -> u8 {
        match self {
            Self::ProviderAccepted => 1,
            Self::Delivered => 2,
            Self::Read => 3,
            _ => 0,
        }
    }

    fn state(self) -> DeliveryState {
        match self {
            Self::Queued => DeliveryState::Queued,
            Self::ProviderAccepted => DeliveryState::ProviderAccepted,
            Self::Delivered => DeliveryState::Delivered,
            Self::Read => DeliveryState::Read,
            Self::Failed => DeliveryState::Failed,
            Self::Uncertain => DeliveryState::Uncertain,
            Self::Cancelled => DeliveryState::Cancelled,
            Self::Expired => DeliveryState::Expired,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Attempt {
    intent_id: String,
    state: Observed,
    rendered: Option<Rendered>,
    receipt: Option<Receipt>,
    #[serde(rename = "reason")]
    _reason: Option<String>,
    retry_after: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    receipt_id: Option<String>,
    intent_id: String,
    state: Observed,
    observed_at: String,
    provider_reference: Option<String>,
    detail: Option<String>,
}

impl Receipt {
    fn valid_attributes(&self) -> bool {
        self.receipt_id
            .as_ref()
            .is_none_or(|id| id.parse::<Uuid>().is_ok())
            && self.provider_reference.as_ref().is_none_or(|value| {
                (1..=1000).contains(&value.chars().count()) && !value.contains('\0')
            })
            && self
                .detail
                .as_ref()
                .is_none_or(|text| text.chars().count() <= 2000)
    }

    fn into_outcome(self) -> Option<ReplySendOutcome> {
        let observation = ReplyObservation {
            state: self.state.state(),
            observed_at: self.observed_at.parse().ok()?,
            provider_reference: self.provider_reference,
            detail: None,
        };
        match self.receipt_id {
            Some(id) => Some(ReplySendOutcome::Receipt(id.parse().ok()?, observation)),
            None => Some(ReplySendOutcome::Observation(observation)),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Rendered {
    intent_id: String,
    texts: Vec<String>,
    #[serde(default)]
    actions: Vec<Value>,
    #[serde(default)]
    attachments: Vec<Value>,
    mode: String,
    reply_to: Option<Value>,
    related_to: Option<Value>,
    medium: String,
}

fn matches(id: Uuid, observed: &str) -> bool {
    observed.parse::<Uuid>().ok() == Some(id)
}

fn valid_time(value: Option<&str>) -> bool {
    value.is_none_or(|value| value.parse::<StoredInstant>().is_ok())
}

impl Attempt {
    fn verifies(&self, intent: Uuid, caps: &Capabilities, text: &str) -> bool {
        let (Some(rendered), Some(receipt)) = (&self.rendered, &self.receipt) else {
            return false;
        };
        matches(intent, &self.intent_id)
            && matches(intent, &receipt.intent_id)
            && matches(intent, &rendered.intent_id)
            && self.state.rank() > 0
            && receipt.state.rank() >= self.state.rank()
            && receipt.valid_attributes()
            && valid_time(Some(&receipt.observed_at))
            && valid_time(self.retry_after.as_deref())
            && rendered.mode == "send"
            && rendered.medium == "text"
            && rendered.actions.is_empty()
            && rendered.attachments.is_empty()
            && rendered.reply_to.is_none()
            && rendered.related_to.is_none()
            && rendered
                .texts
                .iter()
                .all(|text| text.chars().count() <= caps.max_text_length)
            && rendered.texts.concat().contains(text)
    }
}

impl Extension<'_> {
    pub(super) async fn deliver_checkin(
        &self,
        body: Value,
        now: StoredInstant,
    ) -> Option<ReplySendOutcome> {
        let intent = OutboundIntent::normalize(body).ok()?;
        let text = intent.document()["blocks"].as_array()?.first()?["text"].as_str()?;
        let (_cancel, mut cancelled) = watch::channel(false);
        let raw = match self
            .describe_kind(
                "channel",
                &mut cancelled,
                ExtensionError::ChannelCapabilities,
            )
            .await
        {
            Ok(descriptor) => descriptor,
            Err(ExtensionError::ChannelCapabilities) => return Some(cancelled_delivery(now)),
            Err(_) => return preflight_retry(now),
        };
        let Ok(capabilities) = serde_json::from_value::<Capabilities>(raw) else {
            return Some(cancelled_delivery(now));
        };
        if !capabilities.text
            || !capabilities.initiatives
            || !(1..=1_000_000).contains(&capabilities.max_text_length)
        {
            return Some(cancelled_delivery(now));
        }
        let params = json!({"intent":intent.document(),"now":now.to_string()});
        let policy = match self
            .request(
                2,
                "delivery_policy",
                params.clone(),
                MAX_REPLY,
                &mut cancelled,
            )
            .await
        {
            Ok(value) => value,
            Err(_) => return preflight_retry(now),
        };
        let Ok(policy) = serde_json::from_str::<Policy>(policy.get()) else {
            return Some(cancelled_delivery(now));
        };
        if !valid_time(policy.retry_after.as_deref())
            || policy
                .reason
                .as_ref()
                .is_some_and(|reason| reason.chars().count() > 1000)
        {
            return Some(cancelled_delivery(now));
        }
        if !policy.allow_delivery || !policy.allow_initiative {
            return Some(
                policy
                    .retry_after
                    .and_then(|time| time.parse().ok())
                    .map_or_else(|| cancelled_delivery(now), ReplySendOutcome::RetryAt),
            );
        }
        let response = self
            .request(3, "deliver", params, MAX_REPLY, &mut cancelled)
            .await
            .ok()?;
        let attempt: Attempt = serde_json::from_str(response.get()).ok()?;
        if !matches(intent.intent_id(), &attempt.intent_id)
            || !valid_time(attempt.retry_after.as_deref())
        {
            return None;
        }
        if attempt.state.rank() > 0 {
            if !attempt.verifies(intent.intent_id(), &capabilities, text) {
                return None;
            }
            return attempt.receipt?.into_outcome();
        }
        if attempt.state == Observed::Queued && attempt.receipt.is_none() {
            return attempt
                .retry_after
                .and_then(|time| time.parse().ok())
                .map(ReplySendOutcome::RetryAt)
                .or_else(|| retry_after(now, 15 * 60));
        }
        if attempt.receipt.as_ref().is_some_and(|receipt| {
            receipt.state != attempt.state
                || !matches(intent.intent_id(), &receipt.intent_id)
                || !valid_time(Some(&receipt.observed_at))
                || !receipt.valid_attributes()
        }) {
            return None;
        }
        if let Some(receipt) = attempt.receipt {
            return receipt.into_outcome();
        }
        Some(ReplySendOutcome::Observation(ReplyObservation {
            state: attempt.state.state(),
            observed_at: now,
            provider_reference: None,
            detail: None,
        }))
    }

    async fn channel_probe(
        &self,
        cancelled: &mut watch::Receiver<bool>,
    ) -> Result<Value, ExtensionError> {
        let mut parts = self.instance_id.splitn(3, ':');
        if parts.next() != Some("channel") {
            return Err(ExtensionError::ChannelConfiguration);
        }
        let (Some(channel), Some(instance)) = (parts.next(), parts.next()) else {
            return Err(ExtensionError::ChannelConfiguration);
        };
        let intent = OutboundIntent::normalize(json!({
            "owner_id": Uuid::new_v4().to_string(),
            "conversation_id": Uuid::new_v4().to_string(),
            "channel_instance":{"channel":channel,"instance_id":instance},
            "blocks":[{"text":TEXT}]
        }))
        .map_err(|_| ExtensionError::ChannelConfiguration)?;
        let capabilities = self
            .describe_kind("channel", cancelled, ExtensionError::ChannelCapabilities)
            .await?;
        let capabilities: Capabilities = serde_json::from_value(capabilities)
            .map_err(|_| ExtensionError::ChannelCapabilities)?;
        if !capabilities.text || !(1..=1_000_000).contains(&capabilities.max_text_length) {
            return Err(ExtensionError::ChannelCapabilities);
        }
        let params = json!({"intent":intent.document(),"now":NOW});
        let policy = self
            .request(2, "delivery_policy", params.clone(), MAX_REPLY, cancelled)
            .await?;
        let policy: Policy =
            serde_json::from_str(policy.get()).map_err(|_| ExtensionError::ChannelDelivery)?;
        if !policy.allow_delivery
            || (intent.document()["initiative"] == true && !policy.allow_initiative)
            || !valid_time(policy.retry_after.as_deref())
            || policy
                .reason
                .as_ref()
                .is_some_and(|reason| reason.chars().count() > 1000)
        {
            return Err(ExtensionError::ChannelDelivery);
        }
        let attempt = self
            .request(3, "deliver", params, MAX_REPLY, cancelled)
            .await?;
        let attempt: Attempt =
            serde_json::from_str(attempt.get()).map_err(|_| ExtensionError::ChannelDelivery)?;
        if !attempt.verifies(intent.intent_id(), &capabilities, TEXT) {
            return Err(ExtensionError::ChannelDelivery);
        }
        Ok(json!({
            "instance_id": self.instance_id,
            "state": attempt.state,
            "verified_capabilities": ["text"],
            "unverified_capabilities": capabilities.unverified(),
        }))
    }
}

fn cancelled_delivery(at: StoredInstant) -> ReplySendOutcome {
    ReplySendOutcome::Observation(ReplyObservation {
        state: DeliveryState::Cancelled,
        observed_at: at,
        provider_reference: None,
        detail: None,
    })
}

fn preflight_retry(at: StoredInstant) -> Option<ReplySendOutcome> {
    retry_after(at, 15)
}

fn retry_after(at: StoredInstant, delay_seconds: i64) -> Option<ReplySendOutcome> {
    let retry = at
        .unix_microseconds()
        .checked_add(delay_seconds * 1_000_000)?;
    StoredInstant::from_unix_microseconds(retry)
        .ok()
        .map(ReplySendOutcome::RetryAt)
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn check_channel(
    executable: &Path,
    instance_id: &str,
) -> Result<(), Box<dyn Error>> {
    let extension = Extension::new(executable, instance_id)
        .map_err(|_| ExtensionError::ChannelConfiguration)?;
    let (cancel, mut cancelled) = watch::channel(false);
    let report = run_until_shutdown(cancel, PROBE_TIMEOUT, async {
        Ok(extension.channel_probe(&mut cancelled).await?)
    })
    .await?;
    println!("{report}");
    Ok(())
}
