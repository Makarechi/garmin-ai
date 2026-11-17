use std::{error::Error, fmt};

use time::{OffsetDateTime, UtcOffset, format_description::well_known::Rfc3339};

/// An instant at the precision of the Python datetime and PostgreSQL columns.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct StoredInstant(OffsetDateTime);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidStoredInstant;

impl fmt::Display for InvalidStoredInstant {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Expected a microsecond-precision timestamp with an explicit UTC offset")
    }
}

impl Error for InvalidStoredInstant {}

impl StoredInstant {
    /// Interpret a timestamp using the legacy typed application input contract.
    pub fn from_legacy_input(value: &serde_json::Value) -> Option<Self> {
        crate::event_time::aware(value)
    }

    /// Interpret a timestamp and keep its display offset for a timeline boundary.
    pub fn legacy_input_with_offset(value: &serde_json::Value) -> Option<OffsetDateTime> {
        let parsed = crate::event_time::parse_aware_datetime(value)?;
        let offset = UtcOffset::from_whole_seconds(parsed.time.tz_offset?).ok()?;
        let at = OffsetDateTime::from(crate::event_time::as_stored_instant(parsed)?);
        at.checked_to_offset(offset)
    }

    /// Interpret a typed timestamp while retaining its legacy JSON display form.
    pub fn legacy_input_with_json(value: &serde_json::Value) -> Option<(Self, String)> {
        let parsed = crate::event_time::parse_aware_datetime(value)?;
        Some((
            crate::event_time::as_stored_instant(parsed)?,
            parsed.to_string(),
        ))
    }

    pub fn from_unix_microseconds(value: i64) -> Result<Self, InvalidStoredInstant> {
        let time = OffsetDateTime::from_unix_timestamp_nanos(i128::from(value) * 1_000)
            .map_err(|_| InvalidStoredInstant)?;
        time.try_into()
    }

    pub fn unix_microseconds(self) -> i64 {
        // Years 1 through 9999 fit in an i64 at microsecond precision.
        self.0.unix_timestamp() * 1_000_000 + i64::from(self.0.microsecond())
    }
}

impl TryFrom<OffsetDateTime> for StoredInstant {
    type Error = InvalidStoredInstant;

    fn try_from(value: OffsetDateTime) -> Result<Self, Self::Error> {
        if !(1..=9999).contains(&value.year()) || !value.nanosecond().is_multiple_of(1_000) {
            return Err(InvalidStoredInstant);
        }
        let utc = value
            .checked_to_offset(UtcOffset::UTC)
            .ok_or(InvalidStoredInstant)?;
        if !(1..=9999).contains(&utc.year()) {
            return Err(InvalidStoredInstant);
        }
        Ok(Self(utc))
    }
}

impl From<StoredInstant> for OffsetDateTime {
    fn from(value: StoredInstant) -> Self {
        value.0
    }
}

impl std::str::FromStr for StoredInstant {
    type Err = InvalidStoredInstant;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        OffsetDateTime::parse(value, &Rfc3339)
            .map_err(|_| InvalidStoredInstant)?
            .try_into()
    }
}

impl fmt::Display for StoredInstant {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = self.0;
        write!(
            f,
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            value.year(),
            u8::from(value.month()),
            value.day(),
            value.hour(),
            value.minute(),
            value.second()
        )?;
        let micros = value.microsecond();
        if micros != 0 {
            write!(f, ".{micros:06}")?;
        }
        f.write_str("+00:00")
    }
}
