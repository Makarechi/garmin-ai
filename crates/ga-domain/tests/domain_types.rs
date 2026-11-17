use ga_domain::{
    identity::{GarminProfileId, ProfileIdUnavailable},
    text_offsets::{TextOffsets, from_code_points},
    timestamp::{InvalidStoredInstant, StoredInstant},
};

#[test]
fn garmin_profile_fingerprint_matches_python() -> Result<(), ProfileIdUnavailable> {
    let fixture = "70fcd15af12f6adf07d8ca6ada045fa2589715ab50980529cb8626802a8bcb79";
    for input in [
        br#"{"profileId":"0000000000000000042"}"#.as_slice(),
        br#"{"profileId":42}"#,
        br#"{"profileId":true,"profile\u0049d":42}"#,
    ] {
        assert_eq!(
            GarminProfileId::from_profile_json(input, input.len())?.fingerprint(),
            fixture
        );
    }
    let upper_limit: GarminProfileId = "9223372036854775807".parse()?;
    assert_eq!(
        upper_limit.fingerprint(),
        "6129c3ecbe8b994e3250f338ef9b9264287871984342be8e2a9b58e79a2fe222"
    );
    Ok(())
}

#[test]
fn garmin_profile_rejects_invalid_identity_and_never_coerces_values() {
    for input in [
        r#"{"profileId":true}"#,
        r#"{"profileId":42.0}"#,
        r#"{"profileId":false}"#,
        r#"{"profileId":null}"#,
        r#"{"profileId":[]}"#,
        r#"{"profileId":0}"#,
        r#"{"profileId":-42}"#,
        r#"{"profileId":9223372036854775808}"#,
        r#"{"profileId":"9223372036854775808"}"#,
        r#"{"profileId":"00000000000000000042"}"#,
        r#"{"profileId":"+42"}"#,
        r#"{"profileId":" 42"}"#,
        r#"{"profileId":"４２"}"#,
        r#"{"profileId":42,"profileId":false}"#,
        "{}",
        "[]",
    ] {
        assert!(GarminProfileId::from_profile_json(input.as_bytes(), input.len()).is_err());
    }
    let input = br#"{"profileId":42}"#;
    assert!(GarminProfileId::from_profile_json(input, input.len() - 1).is_err());
}

#[test]
fn stored_timestamps_match_python_at_microsecond_precision() -> Result<(), InvalidStoredInstant> {
    for (input, utc, micros) in [
        (
            "2026-10-25T02:30:00+02:00",
            "2026-10-25T00:30:00+00:00",
            1_792_888_200_000_000,
        ),
        (
            "2026-10-25T02:30:00+01:00",
            "2026-10-25T01:30:00+00:00",
            1_792_891_800_000_000,
        ),
        (
            "2026-03-29T01:59:59.000001+01:00",
            "2026-03-29T00:59:59.000001+00:00",
            1_774_745_999_000_001,
        ),
        (
            "1969-12-31T23:59:59.999999Z",
            "1969-12-31T23:59:59.999999+00:00",
            -1,
        ),
        (
            "2000-02-29T22:42:00.999900-03:30",
            "2000-03-01T02:12:00.999900+00:00",
            951_876_720_999_900,
        ),
        (
            "0001-01-01T00:00:00Z",
            "0001-01-01T00:00:00+00:00",
            -62_135_596_800_000_000,
        ),
        (
            "9999-12-31T23:59:59.999999Z",
            "9999-12-31T23:59:59.999999+00:00",
            253_402_300_799_999_999,
        ),
    ] {
        let value = input.parse::<StoredInstant>()?;
        assert_eq!(value.to_string(), utc);
        assert_eq!(value.unix_microseconds(), micros);
        assert_eq!(StoredInstant::from_unix_microseconds(micros)?, value);
    }
    Ok(())
}

#[test]
fn stored_timestamps_reject_loss_of_precision_and_implicit_timezones() {
    for input in [
        "2026-10-25T02:30:00",
        "2026-10-25T02:30:00.0000001+01:00",
        "2016-12-31T23:59:60Z",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ] {
        assert_eq!(input.parse::<StoredInstant>(), Err(InvalidStoredInstant));
    }
    assert_eq!(
        StoredInstant::from_unix_microseconds(i64::MAX),
        Err(InvalidStoredInstant)
    );
}

#[test]
fn code_point_offsets_preserve_non_bmp_and_combining_characters() {
    let text = "A🧪e\u{301}Z";
    for (span, utf8, utf16, quote) in [
        (1..2, 1..5, 1..3, "🧪"),
        (2..4, 5..8, 3..5, "e\u{301}"),
        (5..5, 9..9, 6..6, ""),
        (0..5, 0..9, 0..6, text),
    ] {
        assert_eq!(&text[utf8.clone()], quote);
        assert_eq!(
            from_code_points(text, span),
            Some(TextOffsets { utf8, utf16 })
        );
    }
    assert_eq!(from_code_points(text, 5..6), None);
    assert_eq!(
        from_code_points(text, std::ops::Range { start: 4, end: 1 }),
        None
    );
}
