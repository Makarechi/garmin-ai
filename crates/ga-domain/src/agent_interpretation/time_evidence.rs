mod clock_text;

use jiff::{Timestamp, tz::AmbiguousOffset};
use std::{error::Error, fmt};
use time::{Duration, OffsetDateTime};

use super::{AgentIntent, AgentInterpretation};
use crate::timestamp::StoredInstant;
use clock_text::has_explicit_offset;

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
            let check_end = event.end().is_some()
                && (!correction || changed("end") || self.intent == AgentIntent::Close);
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
