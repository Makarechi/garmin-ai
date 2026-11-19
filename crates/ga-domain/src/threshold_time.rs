//! Elapsed-time slices used by local-hour threshold reports.

use jiff::{Timestamp, civil::Date, tz::TimeZone};

#[derive(Clone, Copy, Debug)]
pub struct ThresholdCalendarError;

pub struct LocalHourSlice {
    pub hour: usize,
    pub day: Date,
    pub seconds: f64,
}

fn local(at: i64, zone: &TimeZone) -> Result<jiff::ZonedDateTime, ThresholdCalendarError> {
    let local = Timestamp::from_microsecond(at)
        .map_err(|_| ThresholdCalendarError)?
        .to_zoned_datetime(zone.clone());
    if !(1..=9999).contains(&local.year()) {
        return Err(ThresholdCalendarError);
    }
    Ok(local)
}

/// Match the reference's hour boundaries, including partial-hour offset changes.
pub fn local_hour_slices(
    start: i64,
    end: i64,
    timezone: &str,
) -> Result<Vec<LocalHourSlice>, ThresholdCalendarError> {
    let zone = jiff::tz::db()
        .get(timezone)
        .map_err(|_| ThresholdCalendarError)?;
    let mut slices = Vec::new();
    let mut left = start;
    while left < end {
        let clock = local(left, &zone)?;
        let micros = 3_600_000_000
            - (i64::from(clock.minute()) * 60_000_000
                + i64::from(clock.second()) * 1_000_000
                + i64::from(clock.millisecond()) * 1_000
                + i64::from(clock.microsecond()));
        let mut stop = end.min(left.checked_add(micros).ok_or(ThresholdCalendarError)?);
        // The reference performs the datetime addition before selecting `end`.
        if !(-62_135_596_800_000_000..=253_402_300_799_999_999).contains(&(left + micros)) {
            return Err(ThresholdCalendarError);
        }
        if local(stop - 1, &zone)?.offset() != clock.offset() {
            let mut low = left;
            let mut high = stop;
            while high - low > 1 {
                let distance = high - low;
                let half = distance / 2;
                // datetime's timedelta division rounds half microseconds to even.
                let middle = low + half + i64::from(distance % 2 != 0 && half % 2 != 0);
                if local(middle, &zone)?.offset() == clock.offset() {
                    low = middle;
                } else {
                    high = middle;
                }
            }
            stop = high;
        }
        slices.push(LocalHourSlice {
            hour: usize::try_from(clock.hour()).map_err(|_| ThresholdCalendarError)?,
            day: clock.date(),
            seconds: (stop - left) as f64 / 1_000_000.0,
        });
        left = stop;
    }
    Ok(slices)
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use serde_json::Value;

    #[test]
    fn matches_frozen_local_hour_slices() -> Result<(), Box<dyn Error>> {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../contracts/threshold-hours.json"))?;
        for (index, case) in cases.iter().enumerate() {
            let actual = super::local_hour_slices(
                case["start"].as_i64().ok_or("Missing start")?,
                case["end"].as_i64().ok_or("Missing end")?,
                case["zone"].as_str().ok_or("Missing zone")?,
            );
            if case["error"].is_string() {
                assert!(actual.is_err(), "Expected calendar error {index}");
                continue;
            }
            let actual = actual.map_err(|_| "Unexpected calendar error")?;
            let expected = case["slices"].as_array().ok_or("Missing slices")?;
            assert_eq!(actual.len(), expected.len(), "Slice count {index}");
            for (actual, expected) in actual.iter().zip(expected) {
                assert_eq!(
                    Some(actual.hour as u64),
                    expected[0].as_u64(),
                    "hour {index}"
                );
                assert_eq!(
                    Some(actual.day.to_string().as_str()),
                    expected[1].as_str(),
                    "day {index}"
                );
                assert_eq!(
                    Some(actual.seconds.to_bits()),
                    expected[2].as_f64().map(f64::to_bits),
                    "seconds {index}"
                );
            }
        }
        Ok(())
    }
}
