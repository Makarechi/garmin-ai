use std::{error::Error, fmt};

use crate::{event_input::EventInput, timestamp::StoredInstant};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EventTopology {
    Point,
    OpenInterval,
    BoundedInterval,
    Flexible,
}

impl EventTopology {
    pub fn label(self) -> &'static str {
        match self {
            Self::Point => "point",
            Self::OpenInterval => "open_interval",
            Self::BoundedInterval => "bounded_interval",
            Self::Flexible => "flexible",
        }
    }

    fn resolved(self, event: &EventInput) -> Self {
        match (self, event.end()) {
            (Self::OpenInterval, None) => Self::OpenInterval,
            (Self::Flexible | Self::OpenInterval, Some(end)) if end > event.start() => {
                Self::BoundedInterval
            }
            (Self::Flexible | Self::OpenInterval, _) => Self::Point,
            (topology, _) => topology,
        }
    }
}

pub struct EventProvenance {
    pub topology: EventTopology,
    pub time_precision: &'static str,
    pub assertion_kind: &'static str,
    pub producer: &'static str,
    pub transport: Option<&'static str>,
    pub author: Option<&'static str>,
    pub validation_status: &'static str,
    pub recorded_at: StoredInstant,
    pub ingested_at: StoredInstant,
    pub clock_uncertainty_seconds: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidClockUncertainty;

impl fmt::Display for InvalidClockUncertainty {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Wearable event clock uncertainty is invalid")
    }
}
impl Error for InvalidClockUncertainty {}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Channel {
    Local,
    Wearable,
    Telegram,
    Mcp,
    Api,
}

impl Channel {
    fn from_actor(actor: Option<&str>, source: &str) -> Self {
        match actor {
            Some(actor) if actor.starts_with("wearable:") => Self::Wearable,
            Some(actor) if actor.starts_with("telegram:") => Self::Telegram,
            Some("mcp") => Self::Mcp,
            Some("api") => Self::Api,
            Some(_) => Self::Local,
            None => match source {
                "wearable" => Self::Wearable,
                "telegram_text" | "telegram_button" | "telegram_voice" => Self::Telegram,
                "mcp" => Self::Mcp,
                _ => Self::Local,
            },
        }
    }
}

impl EventInput {
    /// Derive provenance from the server-authenticated actor and captured clock.
    /// An absent actor is for reconstructing previously stored source information.
    pub fn provenance(
        &self,
        actor: Option<&str>,
        definition_topology: EventTopology,
        recorded: StoredInstant,
        clock_uncertainty_seconds: Option<u32>,
    ) -> Result<EventProvenance, InvalidClockUncertainty> {
        derive_provenance(
            actor,
            self.source().label(),
            self.status().label(),
            definition_topology.resolved(self),
            recorded,
            clock_uncertainty_seconds,
        )
    }
}

/// Reconstruct older audit envelopes where the source may be file import.
pub fn restore_legacy_provenance(
    actor: &str,
    source: &str,
    status: &str,
    topology: EventTopology,
    recorded: StoredInstant,
) -> Option<EventProvenance> {
    if !matches!(
        source,
        "manual"
            | "telegram_text"
            | "telegram_button"
            | "telegram_voice"
            | "mcp"
            | "inferred"
            | "wearable"
            | "file_import"
    ) {
        return None;
    }
    derive_provenance(Some(actor), source, status, topology, recorded, None).ok()
}

pub(crate) fn derive_provenance(
    actor: Option<&str>,
    source: &str,
    status: &str,
    topology: EventTopology,
    recorded: StoredInstant,
    clock_uncertainty_seconds: Option<u32>,
) -> Result<EventProvenance, InvalidClockUncertainty> {
    let channel = Channel::from_actor(actor, source);
    if clock_uncertainty_seconds.is_some_and(|value| {
        value > 31_536_000 || !actor.is_some_and(|actor| actor.starts_with("wearable:"))
    }) {
        return Err(InvalidClockUncertainty);
    }
    let time_precision = if clock_uncertainty_seconds.is_some_and(|value| value != 0) {
        "unknown"
    } else {
        match topology {
            EventTopology::Point => "instant",
            EventTopology::OpenInterval | EventTopology::BoundedInterval => "interval",
            EventTopology::Flexible => "unknown",
        }
    };
    let system_inference = source == "inferred"
        && actor.is_none_or(|actor| actor == "system" || actor.starts_with("system:"));
    let (assertion_kind, producer, transport, author) = match channel {
        Channel::Wearable => ("device_measurement", "wearable", Some("connector"), None),
        Channel::Local if system_inference => ("inferred", "system", None, None),
        Channel::Telegram => {
            let transport = if source.starts_with("telegram_") {
                legacy_transport(source)
            } else {
                "telegram"
            };
            ("user_report", "telegram", Some(transport), Some("owner"))
        }
        Channel::Mcp => ("user_report", "owner", Some("mcp"), Some("owner")),
        Channel::Api => ("user_report", "owner", Some("api"), Some("owner")),
        Channel::Local => (
            "user_report",
            "owner",
            Some(legacy_transport(source)),
            Some("owner"),
        ),
    };
    let validation_status = if status != "confirmed" {
        "needs_confirmation"
    } else if channel == Channel::Wearable {
        "trusted"
    } else {
        "schema_validated"
    };
    Ok(EventProvenance {
        topology,
        time_precision,
        assertion_kind,
        producer,
        transport,
        author,
        validation_status,
        recorded_at: recorded,
        ingested_at: recorded,
        clock_uncertainty_seconds,
    })
}

fn legacy_transport(source: &str) -> &'static str {
    match source {
        "telegram_text" => "telegram_text",
        "telegram_button" => "telegram_button",
        "telegram_voice" => "telegram_voice",
        "wearable" => "wearable",
        "mcp" => "mcp",
        "inferred" => "inferred",
        "file_import" => "file_import",
        _ => "manual",
    }
}
