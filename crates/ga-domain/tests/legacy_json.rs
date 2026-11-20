use ga_domain::{
    legacy_json::{JsonError, Utf8Layout, encode},
    sha256_hex,
};

const INPUT: &[u8] = include_bytes!("fixtures/legacy-json.input");

#[test]
fn matches_python_archive_and_compact_bytes() -> Result<(), JsonError> {
    for (layout, expected, digest) in [
        (
            Utf8Layout::Archive,
            include_bytes!("fixtures/legacy-json.archive").as_slice(),
            "24d8dd0ad99ca1b68513e606fbb9143ca14bffee06bb0dc9dc74d221a0cf54f5",
        ),
        (
            Utf8Layout::Compact,
            include_bytes!("fixtures/legacy-json.compact").as_slice(),
            "214dae150d1f71fc5a5534c6cc0b9e677e9e402f6a4a5d239694f1f50290c6f1",
        ),
    ] {
        let encoded = encode(INPUT, layout, INPUT.len())?;
        assert_eq!(std::str::from_utf8(&encoded), std::str::from_utf8(expected));
        assert_eq!(sha256_hex(&encoded), digest);
    }
    Ok(())
}

#[test]
fn rejects_invalid_nonfinite_and_oversize_input() {
    for input in [b"NaN".as_slice(), b"Infinity", b"-Infinity", b"1e400"] {
        assert_eq!(
            encode(input, Utf8Layout::Archive, input.len()),
            Err(JsonError::NonfiniteNumber)
        );
    }
    for input in [b"true 0".as_slice(), br#""\ud800""#, b"\"\xff\""] {
        assert_eq!(
            encode(input, Utf8Layout::Archive, input.len()),
            Err(JsonError::InvalidJson)
        );
    }
    assert_eq!(
        encode(INPUT, Utf8Layout::Archive, INPUT.len() - 1),
        Err(JsonError::InputTooLarge)
    );
}

#[test]
fn retains_python_integer_digit_limit() -> Result<(), JsonError> {
    let allowed = format!("-{}", "1".repeat(4300));
    let blocked = format!("{allowed}1");
    assert_eq!(
        encode(allowed.as_bytes(), Utf8Layout::Archive, allowed.len())?,
        allowed.as_bytes()
    );
    assert_eq!(
        encode(blocked.as_bytes(), Utf8Layout::Archive, blocked.len()),
        Err(JsonError::InvalidJson)
    );
    Ok(())
}

#[test]
fn canonical_json_stabilizes_after_one_pass_and_keeps_the_exact_byte_limit()
-> Result<(), Box<dyn std::error::Error>> {
    for layout in [
        Utf8Layout::Archive,
        Utf8Layout::Ordered,
        Utf8Layout::Compact,
    ] {
        for text in [
            "",
            "кириллица ☕",
            "e\u{0301}",
            "🧪\n\t\u{0000}",
            "quote \" slash \\",
        ] {
            for number in ["0", "-0.0", "1e-07", "1e+20", "9007199254740993000000"] {
                let text = serde_json::to_string(text)?;
                let input = format!(
                    r#"{{ "z": [null, false, 0, {number}], "текст": {text}, "z": ["last", {number}] }}"#,
                );
                let input = input.as_bytes();
                let encoded = encode(input, layout, input.len())?;
                assert_eq!(
                    encode(&encoded, layout, encoded.len())?,
                    encoded,
                    "{layout:?}, {text:?}, {number}"
                );
                assert_eq!(
                    encode(&encoded, layout, encoded.len() - 1),
                    Err(JsonError::InputTooLarge)
                );
                let parsed: serde_json::Value = serde_json::from_slice(&encoded)?;
                assert_eq!(
                    parsed["текст"],
                    serde_json::from_str::<serde_json::Value>(&text)?
                );
                assert_eq!(parsed["z"][0], "last");
                assert!(parsed["z"][1].is_number());
            }
        }
    }
    Ok(())
}
