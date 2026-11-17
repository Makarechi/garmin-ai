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
