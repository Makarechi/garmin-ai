use std::{error::Error, fmt};

use serde_json::Value;
use time::Date;

use crate::{
    event_input::source_calendar_date,
    garmin_summary::{activity_identity, gmt_instant},
    timestamp::StoredInstant,
};

pub const ACTIVITY_PAGE_SIZE: usize = 100;
const OVERLAP: usize = 20;
const MAX_RESTART_ROUND: u8 = 3;

pub struct ActivityScanPage<'a> {
    pub offset: u32,
    pub round: u8,
    pub anchor: &'a [String],
    pub since: Date,
    pub backfill: bool,
}

#[derive(Debug, Eq, PartialEq)]
pub enum ActivityScanAdvance {
    Following {
        offset: u32,
        round: u8,
        anchor: Vec<String>,
    },
    Unstable {
        next_scan_at: StoredInstant,
    },
    Complete {
        next_scan_at: StoredInstant,
    },
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct InvalidActivityPage;

impl fmt::Display for InvalidActivityPage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Garmin activity scan page could not be interpreted")
    }
}

impl Error for InvalidActivityPage {}

/// Advance a verified current page of mutable offset-based activity history.
pub fn advance_activity_scan(
    cursor: ActivityScanPage<'_>,
    values: &[Value],
    timezone: &str,
    now: StoredInstant,
) -> Result<ActivityScanAdvance, InvalidActivityPage> {
    let ids = values
        .iter()
        .map(activity_identity)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| InvalidActivityPage)?;
    if !ids.starts_with(cursor.anchor) {
        if cursor.round >= MAX_RESTART_ROUND {
            return Ok(ActivityScanAdvance::Unstable {
                next_scan_at: after_seconds(now, 60 * 60)?,
            });
        }
        return Ok(ActivityScanAdvance::Following {
            offset: 0,
            round: cursor.round + 1,
            anchor: Vec::new(),
        });
    }
    if values.len() == ACTIVITY_PAGE_SIZE {
        let observed = gmt_instant(&values[ACTIVITY_PAGE_SIZE - 1]["startTimeGMT"])
            .and_then(|instant| source_calendar_date(instant, timezone))
            .ok_or(InvalidActivityPage)?;
        if observed >= cursor.since {
            let step =
                u32::try_from(ACTIVITY_PAGE_SIZE - OVERLAP).map_err(|_| InvalidActivityPage)?;
            return Ok(ActivityScanAdvance::Following {
                offset: cursor.offset.checked_add(step).ok_or(InvalidActivityPage)?,
                round: cursor.round,
                anchor: ids[ACTIVITY_PAGE_SIZE - OVERLAP..].to_vec(),
            });
        }
    }
    Ok(ActivityScanAdvance::Complete {
        next_scan_at: after_seconds(now, if cursor.backfill { 86_400 } else { 15 * 60 })?,
    })
}

fn after_seconds(now: StoredInstant, seconds: i64) -> Result<StoredInstant, InvalidActivityPage> {
    let micros = now
        .unix_microseconds()
        .checked_add(seconds * 1_000_000)
        .ok_or(InvalidActivityPage)?;
    StoredInstant::from_unix_microseconds(micros).map_err(|_| InvalidActivityPage)
}
