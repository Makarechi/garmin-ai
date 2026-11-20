use ga_domain::timestamp::StoredInstant;

#[test]
fn keeps_python_cursor_calendar_clock_offset_and_fraction_rules() -> Result<(), serde_json::Error> {
    let cases: Vec<(String, Option<i64>)> =
        serde_json::from_str(include_str!("fixtures/python-isoformat.json"))?;
    let mut mismatches = Vec::new();
    for (text, expected) in cases {
        let actual =
            StoredInstant::from_python_isoformat(&text).map(StoredInstant::unix_microseconds);
        if actual != expected {
            mismatches.push(format!(
                "{text:?} expected {expected:?}, received {actual:?}"
            ));
        }
    }
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
    Ok(())
}

#[test]
fn cursor_keeps_the_same_microsecond_across_positive_and_negative_offsets()
-> Result<(), Box<dyn std::error::Error>> {
    use ga_domain::timestamp::OffsetTimestamp;
    use time::{OffsetDateTime, UtcOffset};

    for base in [
        "0001-01-02T00:00:00Z",
        "1969-12-31T23:59:59Z",
        "1970-01-01T00:00:00Z",
        "2000-02-29T23:59:59Z",
        "2026-10-25T01:00:00Z",
        "9999-12-30T23:59:59Z",
    ] {
        let base: StoredInstant = base.parse()?;
        for micros in [0, 1, 50, 100_000, 999_999] {
            let instant = StoredInstant::from_unix_microseconds(base.unix_microseconds() + micros)?;
            assert_eq!(instant.to_string().parse::<StoredInstant>()?, instant);
            for seconds in [-86_399, -12_619, -1, 0, 1, 20_700, 50_400, 86_399] {
                let offset = UtcOffset::from_whole_seconds(seconds)?;
                let shifted = OffsetDateTime::from(instant).to_offset(offset);
                let rendered = OffsetTimestamp(shifted).to_string();
                assert_eq!(
                    StoredInstant::from_python_isoformat(&rendered),
                    Some(instant),
                    "offset {seconds} rendered {rendered}"
                );
            }
        }
    }
    Ok(())
}
