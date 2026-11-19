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
