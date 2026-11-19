use std::{error::Error, path::Path};

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

#[derive(Clone, Copy, Deserialize, Eq, Ord, PartialEq, PartialOrd, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum Observed {
    ProviderAccepted,
    Delivered,
    Read,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Attempt {
    intent_id: String,
    state: Observed,
    rendered: Rendered,
    receipt: Receipt,
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
    fn verifies(&self, intent: Uuid, caps: &Capabilities) -> bool {
        let rendered = &self.rendered;
        let receipt = &self.receipt;
        matches(intent, &self.intent_id)
            && matches(intent, &receipt.intent_id)
            && matches(intent, &rendered.intent_id)
            && receipt.state >= self.state
            && receipt
                .receipt_id
                .as_deref()
                .is_none_or(|id| id.parse::<Uuid>().is_ok())
            && valid_time(Some(&receipt.observed_at))
            && valid_time(self.retry_after.as_deref())
            && receipt.provider_reference.as_ref().is_none_or(|value| {
                (1..=1000).contains(&value.chars().count()) && !value.contains('\0')
            })
            && receipt
                .detail
                .as_ref()
                .is_none_or(|text| text.chars().count() <= 2000)
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
            && rendered.texts.concat().contains(TEXT)
    }
}

impl Extension<'_> {
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
        if !attempt.verifies(intent.intent_id(), &capabilities) {
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
