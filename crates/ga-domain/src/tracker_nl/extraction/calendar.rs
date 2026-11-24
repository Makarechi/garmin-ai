use std::collections::BTreeSet;

use serde_json::{Value, json};
use time::OffsetDateTime;

use super::{
    ExtractionError,
    clock::{self, Clock},
    context,
    patterns::regex,
};
use crate::{
    event_payload::python_whitespace,
    timestamp::{StoredInstant, python_timezone},
};

#[derive(Clone)]
struct Dated {
    start: usize,
    end: usize,
    day: jiff::civil::Date,
    basis: &'static str,
}

enum Calendar {
    Day(jiff::civil::Date, &'static str),
    Refused(Value),
}

fn report(status: &str, basis: Option<&str>, reason: &str) -> Value {
    json!({"status":status,"date_basis":basis,"reason":reason})
}

pub(super) fn explicit_offset(text: &str, seconds: Option<i32>) -> Result<bool, ExtractionError> {
    let Some(seconds) = seconds else {
        return Ok(false);
    };
    if regex("zero_offset")?.is_match(text)? {
        return Ok(seconds == 0);
    }
    for matched in regex("numeric_offset")?.captures_iter(text) {
        let matched = matched?;
        let value = (clock::number(&matched[2])? * 60 + clock::number(&matched[3])?) * 60;
        let negative = matched.get(1).ok_or(ExtractionError::Contract)?.as_str() == "-";
        let value = i32::try_from(value).map_err(|_| ExtractionError::Invalid)?;
        let signed = if negative { -value } else { value };
        if signed == seconds {
            return Ok(true);
        }
    }
    Ok(false)
}

fn associated_day(
    quote: &str,
    clocks: &[Clock],
    dates: &[Dated],
    selected: &[Clock],
    source: &str,
    source_start: usize,
) -> Result<Calendar, ExtractionError> {
    let mut attached: Vec<Vec<&Dated>> = vec![Vec::new(); clocks.len()];
    let mut unattached = Vec::new();
    for dated in dates {
        let left = clocks.iter().rposition(|clock| clock[1] <= dated.start);
        let right = clocks.iter().position(|clock| clock[0] >= dated.end);
        let prefix = match right {
            Some(index) => {
                let gap = &quote[dated.end..clocks[index][0]];
                gap.trim_matches(python_whitespace).is_empty()
                    || regex("prefix_connector")?.is_match(gap)?
            }
            None => false,
        };
        let mut suffix = match left {
            Some(index) => {
                regex("suffix_connector")?.is_match(&quote[clocks[index][1]..dated.start])?
            }
            None => false,
        };
        if prefix && suffix {
            let gap = &quote[clocks[left.ok_or(ExtractionError::Contract)?][1]..dated.start];
            let offset = regex("offset_start")?.find(gap)?;
            let gap = gap[offset.map_or(0, |value| value.end())..].trim_matches(python_whitespace);
            if gap == "," || gap == ":" {
                suffix = false;
            }
        }
        if prefix && suffix {
            return Ok(Calendar::Refused(report(
                "ambiguous",
                None,
                "conflicting_calendar_dates",
            )));
        }
        let target = if prefix {
            right
        } else if suffix {
            left
        } else {
            None
        };
        match target {
            Some(index) => attached[index].push(dated),
            None => unattached.push(dated),
        }
    }
    let mut groups = vec![vec![0]];
    for index in 1..clocks.len() {
        let gap = &quote[clocks[index - 1][1]..clocks[index][0]];
        let offset = regex("offset_start")?.find(gap)?;
        let gap = &gap[offset.map_or(0, |value| value.end())..];
        let between = regex("and")?.is_match(gap)?
            && regex("between")?.is_match(&quote[..clocks[index - 1][0]])?;
        if between || regex("interval")?.is_match(gap)? {
            groups
                .last_mut()
                .ok_or(ExtractionError::Contract)?
                .push(index);
        } else {
            groups.push(vec![index]);
        }
    }
    let mut associated = BTreeSet::new();
    let mut basis = "explicit_date";
    for clock in selected {
        let index = clocks
            .iter()
            .position(|value| value == clock)
            .ok_or(ExtractionError::Contract)?;
        let mut evidence = attached[index].clone();
        if evidence.is_empty() {
            let group = groups
                .iter()
                .find(|group| group.contains(&index))
                .ok_or(ExtractionError::Contract)?;
            evidence = group
                .iter()
                .flat_map(|&index| attached[index].iter().copied())
                .collect();
        }
        let Some(first) = evidence.first() else {
            return Ok(Calendar::Refused(report(
                "unverified",
                None,
                "calendar_day_not_evidenced",
            )));
        };
        if evidence.iter().any(|dated| dated.day != first.day)
            || unattached.iter().any(|dated| dated.day != first.day)
        {
            return Ok(Calendar::Refused(report(
                "ambiguous",
                None,
                "conflicting_calendar_dates",
            )));
        }
        for dated in &evidence {
            if context::approximate(source, source_start + dated.start, source_start + dated.end)? {
                return Ok(Calendar::Refused(report(
                    "ambiguous",
                    None,
                    "approximate_time",
                )));
            }
        }
        associated.insert(first.day);
        basis = first.basis;
    }
    if associated.len() > 1 {
        return Ok(Calendar::Refused(report(
            "ambiguous",
            None,
            "clock_has_multiple_dates",
        )));
    }
    Ok(Calendar::Day(
        *associated.first().ok_or(ExtractionError::Contract)?,
        basis,
    ))
}

pub(super) struct DatetimeSource<'a> {
    pub quote: &'a str,
    pub source: &'a str,
    pub source_start: usize,
    pub timezone: &'a str,
    pub now: StoredInstant,
    pub interval_hyphens: bool,
    pub interval_clocks: Option<[[usize; 2]; 2]>,
    pub non_temporal: &'a BTreeSet<(usize, usize)>,
}

pub(super) fn local_datetime(
    instant: StoredInstant,
    zone: &jiff::tz::TimeZone,
) -> Result<(OffsetDateTime, jiff::civil::DateTime), ExtractionError> {
    let offset = crate::event_schema::chat_form::timezone_offset(zone, instant)
        .map_err(|_| ExtractionError::Invalid)?;
    let local = OffsetDateTime::from(instant)
        .checked_to_offset(offset)
        .ok_or(ExtractionError::Invalid)?;
    if !(1..=9999).contains(&local.year()) {
        return Err(ExtractionError::Invalid);
    }
    let civil = jiff::civil::DateTime::new(
        local
            .year()
            .try_into()
            .map_err(|_| ExtractionError::Invalid)?,
        local.month() as i8,
        local
            .day()
            .try_into()
            .map_err(|_| ExtractionError::Invalid)?,
        local
            .hour()
            .try_into()
            .map_err(|_| ExtractionError::Invalid)?,
        local
            .minute()
            .try_into()
            .map_err(|_| ExtractionError::Invalid)?,
        local
            .second()
            .try_into()
            .map_err(|_| ExtractionError::Invalid)?,
        local
            .nanosecond()
            .try_into()
            .map_err(|_| ExtractionError::Invalid)?,
    )
    .map_err(|_| ExtractionError::Invalid)?;
    Ok((local, civil))
}

pub(super) fn evidence(
    value: OffsetDateTime,
    input: DatetimeSource<'_>,
) -> Result<Value, ExtractionError> {
    let DatetimeSource {
        quote,
        source,
        source_start,
        timezone,
        now,
        interval_hyphens,
        interval_clocks,
        non_temporal,
    } = input;
    let zone = python_timezone(timezone).ok_or(ExtractionError::Invalid)?;
    let instant = StoredInstant::try_from(value).map_err(|_| ExtractionError::Invalid)?;
    let (local, civil) = local_datetime(instant, &zone)?;
    let (_, current) = local_datetime(now, &zone)?;
    let (mut clocks, ranges) = clock::tokens(quote, interval_hyphens, interval_clocks)?;
    let mut matching: Vec<_> = clocks
        .iter()
        .copied()
        .filter(|clock| {
            clock[2] == local.hour() as usize
                && clock[3] == local.minute() as usize
                && !non_temporal.contains(&(source_start + clock[0], source_start + clock[1]))
        })
        .collect();
    let now_evidenced = regex("now")?.find(quote)?;
    if let Some(found) = &now_evidenced {
        if (i128::from(instant.unix_microseconds()) - i128::from(now.unix_microseconds())).abs()
            > 120_000_000
        {
            return Ok(report("contradictory", None, "message_time_mismatch"));
        }
        if clocks.is_empty() {
            clocks = vec![[
                found.start(),
                found.end(),
                local.hour() as usize,
                local.minute() as usize,
            ]];
            matching.clone_from(&clocks);
        }
    } else if local.second() != 0 || local.nanosecond() != 0 {
        return Ok(report("contradictory", None, "unsupported_clock_precision"));
    }
    if matching.is_empty() {
        return Ok(report("unverified", None, "clock_not_evidenced"));
    }
    for clock in &matching {
        if context::approximate(source, source_start + clock[0], source_start + clock[1])? {
            return Ok(report("ambiguous", None, "approximate_time"));
        }
    }
    let repeated = matches!(
        zone.to_ambiguous_timestamp(civil).offset(),
        jiff::tz::AmbiguousOffset::Fold { .. }
    ) && now_evidenced.is_none();
    let mut offset_clocks = Vec::new();
    for clock in &matching {
        let suffix = regex("offset_start")?.captures(&quote[clock[1]..])?;
        let suffix = suffix.filter(|matched| {
            !matched
                .get(1)
                .is_some_and(|sign| ranges.contains(&(clock[1] + sign.start())))
        });
        let supported = match suffix {
            Some(suffix) => explicit_offset(
                suffix.get(0).ok_or(ExtractionError::Contract)?.as_str(),
                Some(local.offset().whole_seconds()),
            )?,
            None => !repeated,
        };
        if supported {
            offset_clocks.push(*clock);
        }
    }
    if offset_clocks.is_empty() {
        return Ok(report(
            if repeated {
                "ambiguous"
            } else {
                "contradictory"
            },
            None,
            if repeated {
                "repeated_wall_time"
            } else {
                "clock_offset_mismatch"
            },
        ));
    }
    matching = offset_clocks;
    let mut dates = Vec::new();
    for matched in regex("iso_day")?.find_iter(quote) {
        let matched = matched?;
        let day = if matched.as_str().is_ascii() {
            matched
                .as_str()
                .parse::<jiff::civil::Date>()
                .ok()
                .filter(|day| day.year() >= 1)
        } else {
            None
        };
        let Some(day) = day else {
            return Ok(report("contradictory", None, "invalid_calendar_date"));
        };
        dates.push(Dated {
            start: matched.start(),
            end: matched.end(),
            day,
            basis: "explicit_date",
        });
    }
    for matched in regex("local_day")?.captures_iter(quote) {
        let matched = matched?;
        let day = jiff::civil::Date::new(
            clock::number(&matched[3])?
                .try_into()
                .map_err(|_| ExtractionError::Invalid)?,
            clock::number(&matched[2])?
                .try_into()
                .map_err(|_| ExtractionError::Invalid)?,
            clock::number(&matched[1])?
                .try_into()
                .map_err(|_| ExtractionError::Invalid)?,
        )
        .ok();
        let Some(day) = day.filter(|day| day.year() >= 1) else {
            return Ok(report("contradictory", None, "invalid_calendar_date"));
        };
        let whole = matched.get(0).ok_or(ExtractionError::Contract)?;
        dates.push(Dated {
            start: whole.start(),
            end: whole.end(),
            day,
            basis: "explicit_date",
        });
    }
    for (term, offset) in [
        ("сегодня", 0),
        ("today", 0),
        ("вчера", -1),
        ("yesterday", -1),
        ("завтра", 1),
        ("tomorrow", 1),
    ] {
        for matched in regex(term)?.find_iter(quote) {
            let matched = matched?;
            let day = current
                .date()
                .checked_add(jiff::Span::new().days(offset))
                .map_err(|_| ExtractionError::Invalid)?;
            dates.push(Dated {
                start: matched.start(),
                end: matched.end(),
                day,
                basis: "relative_day",
            });
        }
    }
    if !dates.is_empty() {
        dates.sort_by_key(|date| (date.start, date.end, date.day, date.basis));
        return Ok(
            match associated_day(quote, &clocks, &dates, &matching, source, source_start)? {
                Calendar::Refused(result) => result,
                Calendar::Day(day, basis) if day == civil.date() => {
                    report("supported", Some(basis), "calendar_and_clock")
                }
                Calendar::Day(_, _) => report("contradictory", None, "calendar_date_mismatch"),
            },
        );
    }
    Ok(if now_evidenced.is_some() {
        report("supported", Some("message_time"), "message_time")
    } else {
        report("unverified", None, "calendar_day_not_evidenced")
    })
}
