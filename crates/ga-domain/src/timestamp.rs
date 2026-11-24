use std::{error::Error, fmt};

use time::{OffsetDateTime, PrimitiveDateTime, UtcOffset, format_description::well_known::Rfc3339};

mod python_isoformat;

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
    /// Parse an aware legacy Python cursor timestamp at its retained microsecond precision.
    #[must_use]
    pub fn from_python_isoformat(value: &str) -> Option<Self> {
        Self::from_unix_microseconds(python_isoformat_microseconds(value)?).ok()
    }

    /// Interpret a timestamp using the legacy typed application input contract.
    #[must_use]
    pub fn from_legacy_input(value: &serde_json::Value) -> Option<Self> {
        crate::event_time::aware(value)
    }

    /// Interpret a timestamp and keep its display offset for a timeline boundary.
    #[must_use]
    pub fn legacy_input_with_offset(value: &serde_json::Value) -> Option<OffsetDateTime> {
        let parsed = crate::event_time::parse_aware_datetime(value)?;
        let offset = UtcOffset::from_whole_seconds(parsed.time.tz_offset?).ok()?;
        let at = OffsetDateTime::from(crate::event_time::as_stored_instant(parsed)?);
        at.checked_to_offset(offset)
    }

    /// Interpret a typed timestamp while retaining its legacy JSON display form.
    #[must_use]
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

    #[must_use]
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

/// Parse a legacy cursor's UTC position, including offsets crossing the storage calendar bounds.
#[must_use]
pub fn python_isoformat_microseconds(value: &str) -> Option<i64> {
    Some(python_isoformat::parse(value)?.utc_microseconds())
}

/// Parse the UTC position and retain Python's ISO spelling for a carried-forward cursor.
#[must_use]
pub fn python_isoformat_with_display(value: &str) -> Option<(i64, String)> {
    let parsed = python_isoformat::parse(value)?;
    Some((parsed.utc_microseconds(), parsed.to_string()))
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
        OffsetTimestamp(self.0).fmt(f)
    }
}

/// ISO display with the numeric offset and the stored microsecond precision.
pub struct OffsetTimestamp(pub OffsetDateTime);

impl fmt::Display for OffsetTimestamp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt_local(f, PrimitiveDateTime::new(self.0.date(), self.0.time()))?;
        fmt_offset(f, i64::from(self.0.offset().whole_seconds()) * 1_000_000)
    }
}

fn fmt_local(f: &mut fmt::Formatter<'_>, value: PrimitiveDateTime) -> fmt::Result {
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
    Ok(())
}

fn fmt_offset(f: &mut fmt::Formatter<'_>, offset: i64) -> fmt::Result {
    let sign = if offset < 0 { '-' } else { '+' };
    let seconds = offset.unsigned_abs() / 1_000_000;
    let micros = offset.unsigned_abs() % 1_000_000;
    write!(f, "{sign}{:02}:{:02}", seconds / 3600, seconds / 60 % 60)?;
    if !seconds.is_multiple_of(60) || micros != 0 {
        write!(f, ":{:02}", seconds % 60)?;
    }
    if micros != 0 {
        write!(f, ".{micros:06}")?;
    }
    Ok(())
}

/// Resolve a regional wall clock using Python's first-fold and pre-gap offsets.
#[must_use]
pub fn regional_clock_instant(
    clock: time::PrimitiveDateTime,
    timezone: &str,
) -> Option<OffsetDateTime> {
    let zone = python_timezone(timezone)?;
    let civil = jiff::civil::DateTime::new(
        clock.year().try_into().ok()?,
        clock.month() as i8,
        clock.day().try_into().ok()?,
        clock.hour().try_into().ok()?,
        clock.minute().try_into().ok()?,
        clock.second().try_into().ok()?,
        clock.nanosecond().try_into().ok()?,
    )
    .ok()?;
    let timestamp = zone.to_ambiguous_timestamp(civil).compatible().ok()?;
    OffsetDateTime::from_unix_timestamp_nanos(i128::from(timestamp.as_microsecond()) * 1000).ok()
}

/// Match timezone keys supported by the reference PostgreSQL driver's `ZoneInfo` lookup.
#[must_use]
pub fn source_timezone_available(name: &str) -> bool {
    python_timezone(name).is_some()
}

pub(crate) fn python_timezone(name: &str) -> Option<jiff::tz::TimeZone> {
    let base = name
        .strip_prefix("posix/")
        .or_else(|| name.strip_prefix("right/"))
        .unwrap_or(name);
    if !crate::event_input::known_timezone(base) {
        return None;
    }
    let zone = jiff::tz::db().get(base).ok()?;
    if !name.starts_with("right/") || zone.to_fixed_offset().is_ok() {
        return Some(zone);
    }
    // ZoneInfo ignores leap records but retains the right/ file's transition table.
    jiff::tz::TimeZoneDatabase::from_dir("/usr/share/zoneinfo/right")
        .ok()?
        .get(base)
        .ok()
}
