use std::ops::Range;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TextOffsets {
    pub utf8: Range<usize>,
    pub utf16: Range<usize>,
}

/// Convert a half-open span indexed by Python Unicode code points into byte
/// offsets and into the UTF-16 offsets used by Telegram message entities.
#[must_use]
pub fn from_code_points(text: &str, span: Range<usize>) -> Option<TextOffsets> {
    if span.start > span.end {
        return None;
    }
    let boundaries = text
        .char_indices()
        .map(|(byte, character)| (byte, character.len_utf16()))
        .chain(std::iter::once((text.len(), 0)));
    let mut utf16 = 0;
    let mut start = None;
    for (index, (byte, units)) in boundaries.enumerate() {
        if index == span.start {
            start = Some((byte, utf16));
        }
        if index == span.end {
            let (start_byte, start_utf16) = start?;
            return Some(TextOffsets {
                utf8: start_byte..byte,
                utf16: start_utf16..utf16,
            });
        }
        utf16 += units;
    }
    None
}
