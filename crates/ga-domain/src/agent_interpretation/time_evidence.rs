use std::{error::Error, fmt, sync::OnceLock};

use fancy_regex::Regex;
use jiff::{Timestamp, tz::AmbiguousOffset};
use time::{Duration, OffsetDateTime};

use super::{AgentIntent, AgentInterpretation};
use crate::timestamp::StoredInstant;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimeClarification {
    MissingTargetTimezone,
    TimezoneMismatch,
    AmbiguousTime,
    FutureTime,
}

impl fmt::Display for TimeClarification {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::MissingTargetTimezone => "The selected entry's timezone is required",
            Self::TimezoneMismatch => "The reported timestamp and timezone disagree",
            Self::AmbiguousTime => "The reported local time requires an explicit offset",
            Self::FutureTime => "The reported time is in the future",
        })
    }
}
impl Error for TimeClarification {}

impl AgentInterpretation {
    /// Verify the model's timestamps against the exact authenticated message
    /// and the timezone from the current entry when applying a correction.
    pub fn verify_time_evidence(
        &self,
        message_text: &str,
        reference: StoredInstant,
        target_timezone: Option<&str>,
    ) -> Result<(), TimeClarification> {
        let limit = OffsetDateTime::from(reference).saturating_add(Duration::minutes(5));
        let changed = |field| self.changed_fields.iter().any(|name| name == field);
        for (index, (event, times)) in self.events.iter().zip(&self.reported_times).enumerate() {
            let correction = index == 0
                && matches!(
                    self.intent,
                    AgentIntent::Update | AgentIntent::Close | AgentIntent::Acknowledge
                )
                && !(self.intent == AgentIntent::Acknowledge
                    && event.payload().kind() == "symptom_observation");
            let check_start = !correction || changed("start");
            let check_end = !correction || changed("end") || self.intent == AgentIntent::Close;
            let timezone = if correction && !changed("timezone") {
                target_timezone.ok_or(TimeClarification::MissingTargetTimezone)?
            } else {
                event.timezone()
            };
            let zone = jiff::tz::db()
                .get(timezone)
                .map_err(|_| TimeClarification::TimezoneMismatch)?;
            // EventInput validated the instant. Keep the submitted offset here,
            // because the stored UTC instant cannot prove what the model wrote.
            if check_start && times[0].is_none() {
                return Err(TimeClarification::TimezoneMismatch);
            }
            let checked = times
                .iter()
                .zip([check_start, check_end])
                .filter_map(|(value, check)| check.then_some(*value).flatten());
            for instant in checked.clone() {
                let local = Timestamp::from_microsecond(
                    StoredInstant::try_from(instant)
                        .map_err(|_| TimeClarification::TimezoneMismatch)?
                        .unix_microseconds(),
                )
                .map_err(|_| TimeClarification::TimezoneMismatch)?
                .to_zoned_datetime(zone.clone());
                if instant.offset().whole_seconds() != local.offset().seconds() {
                    return Err(TimeClarification::TimezoneMismatch);
                }
                if matches!(
                    zone.to_ambiguous_timestamp(local.datetime()).offset(),
                    AmbiguousOffset::Fold { .. }
                ) && !has_explicit_offset(message_text, instant)
                {
                    return Err(TimeClarification::AmbiguousTime);
                }
            }
            if checked.into_iter().any(|instant| instant > limit) {
                return Err(TimeClarification::FutureTime);
            }
        }
        Ok(())
    }
}

fn has_explicit_offset(text: &str, at: OffsetDateTime) -> bool {
    static CLOCK: OnceLock<Option<Regex>> = OnceLock::new();
    let Some(pattern) = CLOCK.get_or_init(|| Regex::new(
        r"(?i)(?<![\d:+-])(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?::[0-5]\d(?:\.\d+)?)?\s*(?:(?:UTC|GMT)\s*)?(?P<offset>[+-]\d{2}:?\d{2})?"
    ).ok()) else { return false; };
    let offset = at.offset().whole_seconds();
    if offset % 60 != 0 {
        return false;
    }
    let magnitude = offset.unsigned_abs() / 60;
    let sign = if offset < 0 { '-' } else { '+' };
    let plain = format!("{sign}{:02}{:02}", magnitude / 60, magnitude % 60);
    let colon = format!("{sign}{:02}:{:02}", magnitude / 60, magnitude % 60);
    let mut found = false;
    for entry in pattern.captures_iter(text) {
        let Ok(parts) = entry else {
            return false;
        };
        let (Some(hour), Some(minute)) = (
            parts
                .name("hour")
                .and_then(|hour| hour.as_str().parse::<u8>().ok()),
            parts
                .name("minute")
                .and_then(|minute| minute.as_str().parse::<u8>().ok()),
        ) else {
            // An unrecognized numeric spelling cannot establish unique evidence.
            return false;
        };
        if (hour, minute) != (at.hour(), at.minute()) {
            continue;
        }
        let Some(explicit) = parts.name("offset") else {
            return false;
        };
        if explicit.as_str() != plain && explicit.as_str() != colon {
            return false;
        }
        found = true;
    }
    found
}
