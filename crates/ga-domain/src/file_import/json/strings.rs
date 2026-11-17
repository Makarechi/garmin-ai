use std::borrow::Cow;

use super::{ImportFileError, JsonRowIssue};

const MARKER: char = '\u{f0000}';

/// Jiter accepts Unicode scalars. Keep Python's unpaired escapes distinguishable
/// long enough to reject just their row. Double any real occurrence of the marker.
pub(super) fn escape_unpaired(input: &str) -> Result<Cow<'_, str>, ImportFileError> {
    if !input.contains("\\u") && !input.contains(MARKER) {
        return Ok(Cow::Borrowed(input));
    }
    let bytes = input.as_bytes();
    let mut marker_bytes = [0; 4];
    let marker_bytes = MARKER.encode_utf8(&mut marker_bytes).as_bytes();
    let mut quoted = false;
    let mut output = String::new();
    let mut copied = 0;
    let mut index = 0;
    while let Some(&byte) = bytes.get(index) {
        match byte {
            b'"' => {
                quoted = !quoted;
                index += 1;
            }
            b'\\' if quoted => {
                let unit = if bytes.get(index + 1) == Some(&b'u') {
                    hex_unit(bytes, index)
                } else {
                    None
                };
                match unit {
                    Some(unit @ 0xd800..=0xdfff) => {
                        let low = if (0xd800..=0xdbff).contains(&unit)
                            && bytes.get(index + 6..index + 8) == Some(b"\\u")
                        {
                            hex_unit(bytes, index + 6).filter(|low| (0xdc00..=0xdfff).contains(low))
                        } else {
                            None
                        };
                        if let Some(low) = low {
                            if (unit, low) == (0xdb80, 0xdc00) {
                                output.push_str(&input[copied..index]);
                                output.push(MARKER);
                                output.push(MARKER);
                                copied = index + 12;
                            }
                            index += 12;
                        } else {
                            output.push_str(&input[copied..index]);
                            output.push(MARKER);
                            output.push(
                                char::from_u32(0xe000 + u32::from(unit - 0xd800))
                                    .ok_or(ImportFileError::InvalidFormat)?,
                            );
                            index += 6;
                            copied = index;
                        }
                    }
                    Some(_) => index += 6,
                    None => index += 2,
                }
            }
            _ if quoted && bytes.get(index..index + 4) == Some(marker_bytes) => {
                output.push_str(&input[copied..index]);
                output.push(MARKER);
                output.push(MARKER);
                index += 4;
                copied = index;
            }
            _ => index += 1,
        }
    }
    if copied == 0 {
        Ok(Cow::Borrowed(input))
    } else {
        output.push_str(&input[copied..]);
        Ok(Cow::Owned(output))
    }
}

fn hex_unit(bytes: &[u8], index: usize) -> Option<u16> {
    bytes.get(index + 2..index + 6)?.iter().try_fold(0, |n, b| {
        char::from(*b)
            .to_digit(16)
            .map(|digit| n * 16 + digit as u16)
    })
}

pub(super) fn decode(text: &str) -> (Option<Cow<'_, str>>, Option<JsonRowIssue>) {
    let nul = text.contains('\0').then_some(JsonRowIssue::NullCharacter);
    if !text.contains(MARKER) {
        return (Some(Cow::Borrowed(text)), nul);
    }
    let mut decoded = String::with_capacity(text.len());
    let mut tail = text;
    while let Some((before, after)) = tail.split_once(MARKER) {
        decoded.push_str(before);
        let Some(after) = after.strip_prefix(MARKER) else {
            return (None, nul.or(Some(JsonRowIssue::InvalidUnicode)));
        };
        decoded.push(MARKER);
        tail = after;
    }
    decoded.push_str(tail);
    (Some(Cow::Owned(decoded)), nul)
}
