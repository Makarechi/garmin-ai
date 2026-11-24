use std::collections::BTreeSet;

use super::{ExtractionError, patterns::regex};

pub(super) type Clock = [usize; 4];

pub(super) fn number(text: &str) -> Result<usize, ExtractionError> {
    text.chars()
        .try_fold(0usize, |value, ch| {
            value
                .checked_mul(10)?
                .checked_add(usize::from(crate::metric_contract::decimal_value(ch)?))
        })
        .ok_or(ExtractionError::Invalid)
}

pub(super) fn tokens(
    quote: &str,
    interval_hyphens: bool,
    interval_clocks: Option<[[usize; 2]; 2]>,
) -> Result<(Vec<Clock>, BTreeSet<usize>), ExtractionError> {
    let mut clocks = Vec::new();
    for matched in regex("clock")?.captures_iter(quote) {
        let matched = matched?;
        let whole = matched.get(0).ok_or(ExtractionError::Contract)?;
        clocks.push([
            whole.start(),
            whole.end(),
            number(&matched[1])?,
            number(&matched[2])?,
        ]);
    }
    for matched in regex("at_clock")?.captures_iter(quote) {
        let matched = matched?;
        let hour = matched.get(1).ok_or(ExtractionError::Contract)?;
        let minute = matched.get(2);
        clocks.push([
            hour.start(),
            minute.as_ref().map_or(hour.end(), fancy_regex::Match::end),
            number(hour.as_str())?,
            minute
                .map(|value| number(value.as_str()))
                .transpose()?
                .unwrap_or(0),
        ]);
    }
    clocks.sort_unstable();
    clocks.dedup();
    let mut tokens = Vec::new();
    let mut ranges = BTreeSet::new();
    let mut offset_end = 0;
    for (index, &clock) in clocks.iter().enumerate() {
        if clock[0] < offset_end {
            continue;
        }
        if quote[..clock[0]].ends_with('-') && !ranges.contains(&(clock[0] - 1)) {
            continue;
        }
        tokens.push(clock);
        let offset = regex("offset_start")?.find(&quote[clock[1]..])?;
        let boundary = offset
            .as_ref()
            .map_or(clock[1], |value| clock[1] + value.end());
        let mut next = clocks.get(index + 1);
        let mut separator = clock[1];
        let mut offset_range = false;
        if offset.is_some()
            && let Some(following) = clocks[index + 1..]
                .iter()
                .find(|candidate| candidate[0] >= boundary)
            && regex("hyphen")?.is_match(&quote[boundary..following[0]])?
        {
            next = Some(following);
            separator = boundary;
            offset_range = true;
        }
        let gap = next.map_or("", |next| &quote[separator..next[0]]);
        let matching = interval_clocks.is_none_or(|wanted| {
            [clock[2], clock[3]] == wanted[0]
                && next.is_some_and(|next| [next[2], next[3]] == wanted[1])
        });
        if interval_hyphens
            && regex("hyphen")?.is_match(gap)?
            && !quote[..clock[0]].ends_with(['T', 't'])
            && matching
        {
            ranges.insert(separator + gap.find('-').ok_or(ExtractionError::Contract)?);
            if offset_range {
                offset_end = boundary;
            }
        } else if offset.is_some() {
            offset_end = boundary;
        }
    }
    Ok((tokens, ranges))
}

pub(super) fn temporal_spans(text: &str) -> Result<Vec<(usize, usize)>, ExtractionError> {
    let mut spans = Vec::new();
    for name in ["date_number", "colon_time", "ampm_number"] {
        for matched in regex(name)?.find_iter(text) {
            let matched = matched?;
            spans.push((matched.start(), matched.end()));
        }
    }
    let direct: BTreeSet<_> = regex("before_clock")?
        .find_iter(text)
        .map(|matched| matched.map(|value| value.end()))
        .collect::<Result<_, _>>()?;
    let between: BTreeSet<_> = regex("between_start")?
        .find_iter(text)
        .map(|matched| matched.map(|value| value.end()))
        .collect::<Result<_, _>>()?;
    let (clocks, _) = tokens(text, true, None)?;
    let mut contextual = Vec::new();
    for clock in &clocks {
        contextual.push(
            direct.contains(&clock[0])
                || regex("after_clock")?.is_match(&text[clock[1]..])?
                || regex("ampm_suffix")?.is_match(&text[clock[1]..])?,
        );
    }
    let mut group = 0;
    for index in 0..clocks.len() {
        if let Some(next) = clocks.get(index + 1) {
            let gap = &text[clocks[index][1]..next[0]];
            let offset = regex("offset_start")?.find(gap)?;
            let gap = &gap[offset.map_or(0, |value| value.end())..];
            let linked = regex("interval")?.is_match(gap)?
                || regex("and")?.is_match(gap)? && between.contains(&clocks[index][0]);
            if linked {
                continue;
            }
        }
        if contextual[group..=index].iter().any(|&value| value) {
            spans.extend(
                clocks[group..=index]
                    .iter()
                    .map(|clock| (clock[0], clock[1])),
            );
        }
        group = index + 1;
    }
    Ok(spans)
}
