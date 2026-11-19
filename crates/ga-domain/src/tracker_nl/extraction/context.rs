use super::{ExtractionError, patterns::regex};

fn sentence(
    source: &str,
    start: usize,
    end: usize,
) -> Result<std::ops::Range<usize>, ExtractionError> {
    let mut before = 0;
    let mut after = source.len();
    for matched in regex("sentence")?.find_iter(source) {
        let matched = matched?;
        if matched.end() <= start {
            before = matched.end();
        }
        if matched.start() >= end {
            after = matched.start();
            break;
        }
    }
    Ok(before..after)
}

pub(super) fn uncertain(source: &str, start: usize, end: usize) -> Result<bool, ExtractionError> {
    let range = sentence(source, start, end)?;
    let sentence = &source[range];
    if regex("future")?.is_match(sentence)? || regex("other")?.is_match(sentence)? {
        return Ok(true);
    }
    let mut stack: Vec<(usize, &str)> = Vec::new();
    for (index, ch) in source.char_indices() {
        if stack
            .last()
            .is_some_and(|(_, closing)| closing.contains(ch))
        {
            let (begin, _) = stack.pop().ok_or(ExtractionError::Contract)?;
            if begin < end && index + ch.len_utf8() > start {
                return Ok(true);
            }
        } else if let Some(closing) = match ch {
            '"' => Some("\""),
            '«' => Some("»"),
            '“' => Some("”"),
            '„' => Some("“”"),
            '「' => Some("」"),
            '『' => Some("』"),
            _ => None,
        } {
            stack.push((index, closing));
        }
    }
    Ok(stack.iter().any(|(begin, _)| *begin < end))
}

pub(super) fn negated(source: &str, start: usize, end: usize) -> Result<bool, ExtractionError> {
    let range = sentence(source, start, end)?;
    let start = start
        .checked_sub(range.start)
        .ok_or(ExtractionError::Invalid)?;
    let end = end
        .checked_sub(range.start)
        .ok_or(ExtractionError::Invalid)?;
    let sentence = &source[range];
    let clipped = format!(
        "{} {}",
        sentence.get(..start).ok_or(ExtractionError::Invalid)?,
        sentence.get(end..).ok_or(ExtractionError::Invalid)?
    );
    Ok(regex("negation")?.is_match(&clipped)?)
}

pub(super) fn approximate(source: &str, start: usize, end: usize) -> Result<bool, ExtractionError> {
    Ok(
        regex("approx_before")?.is_match(source.get(..start).ok_or(ExtractionError::Invalid)?)?
            || regex("approx_after")?
                .is_match(source.get(end..).ok_or(ExtractionError::Invalid)?)?,
    )
}
