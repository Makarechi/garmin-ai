use std::borrow::Cow;

#[derive(Clone, Copy)]
enum Encoding {
    Utf8,
    Utf16(bool),
    Utf32(bool),
}

pub(super) fn decode_text(bytes: &[u8]) -> Option<Cow<'_, str>> {
    let (encoding, bom) = detect(bytes);
    let bytes = &bytes[bom..];
    match encoding {
        Encoding::Utf8 => std::str::from_utf8(bytes).ok().map(Cow::Borrowed),
        Encoding::Utf16(little) => {
            let (words, remainder) = bytes.as_chunks::<2>();
            if !remainder.is_empty() {
                return None;
            }
            let units = words.iter().map(|word| {
                if little {
                    u16::from_le_bytes(*word)
                } else {
                    u16::from_be_bytes(*word)
                }
            });
            char::decode_utf16(units)
                .collect::<Result<String, _>>()
                .ok()
                .map(Cow::Owned)
        }
        Encoding::Utf32(little) => {
            let (words, remainder) = bytes.as_chunks::<4>();
            if !remainder.is_empty() {
                return None;
            }
            words
                .iter()
                .map(|word| {
                    char::from_u32(if little {
                        u32::from_le_bytes(*word)
                    } else {
                        u32::from_be_bytes(*word)
                    })
                })
                .collect::<Option<String>>()
                .map(Cow::Owned)
        }
    }
}

fn detect(bytes: &[u8]) -> (Encoding, usize) {
    match bytes {
        [0xff, 0xfe, 0, 0, ..] => (Encoding::Utf32(true), 4),
        [0, 0, 0xfe, 0xff, ..] => (Encoding::Utf32(false), 4),
        [0xef, 0xbb, 0xbf, ..] => (Encoding::Utf8, 3),
        [0xff, 0xfe, ..] => (Encoding::Utf16(true), 2),
        [0xfe, 0xff, ..] => (Encoding::Utf16(false), 2),
        [0, 0, _, _, ..] => (Encoding::Utf32(false), 0),
        [0, _, ..] => (Encoding::Utf16(false), 0),
        [_, 0, 0, 0, ..] => (Encoding::Utf32(true), 0),
        [_, 0, ..] => (Encoding::Utf16(true), 0),
        _ => (Encoding::Utf8, 0),
    }
}
