use std::error::Error;

use ga_domain::{
    event_input::{EventInput, ValidationMode},
    event_replay::{ReplayConflict, check_idempotency_key},
};
use serde_json::json;

#[test]
fn old_caffeine_audit_defaults_and_utc_offsets_match_an_identical_retry()
-> Result<(), Box<dyn Error>> {
    let input = br#"{"start":"2026-10-25T02:30:00+02:00","payload":{"type":"caffeine","beverage":"Synthetic coffee","caffeine_mg_estimate":100.0}}"#;
    let event = EventInput::from_json(input, 4096, ValidationMode::NewReport)?;
    let mut original = json!({
        "start": "2026-10-25T00:30:00+00:00",
        "end": null,
        "timezone": "Europe/Bratislava",
        "original_text": null,
        "source": "manual",
        "status": "confirmed",
        "kind": "caffeine",
        "confidence": 1,
        "payload": {"type": "caffeine", "beverage": "Synthetic coffee", "caffeine_mg_estimate": 100},
        "revision": 1
    });
    assert_eq!(event.verify_creation_replay(Some(&original)), Ok(()));
    original["start"] = json!("2026-10-25T02:30:00+01:00");
    assert_eq!(
        event.verify_creation_replay(Some(&original)),
        Err(ReplayConflict::InputChanged)
    );
    original["start"] = json!("2026-10-25T02:30:00+02:00");
    original["payload"]["caffeine_mg_estimate"] = json!(200.0);
    assert_eq!(
        event.verify_creation_replay(Some(&original)),
        Err(ReplayConflict::InputChanged)
    );
    assert_eq!(
        event.verify_creation_replay(None),
        Err(ReplayConflict::AuditUnavailable)
    );
    Ok(())
}

#[test]
fn null_medication_remains_distinct_from_an_explicit_dose() -> Result<(), Box<dyn Error>> {
    let input = br#"{"start":"2026-01-15T11:00:00+00:00","payload":{"type":"medication"}}"#;
    let event = EventInput::from_json(input, 4096, ValidationMode::NewReport)?;
    let mut original = json!({
        "start": event.start().to_string(),
        "end": null,
        "timezone": "Europe/Bratislava",
        "original_text": null,
        "source": "manual",
        "status": "confirmed",
        "kind": "medication",
        "confidence": 1.0,
        "payload": event.payload().json(),
    });
    assert_eq!(event.verify_creation_replay(Some(&original)), Ok(()));
    original["payload"]["dose"] = json!(1.0);
    assert_eq!(
        event.verify_creation_replay(Some(&original)),
        Err(ReplayConflict::InputChanged)
    );
    assert_eq!(check_idempotency_key(""), Err(ReplayConflict::InvalidKey));
    assert_eq!(check_idempotency_key(&"🧪".repeat(200)), Ok(()));
    assert_eq!(
        check_idempotency_key(&"🧪".repeat(201)),
        Err(ReplayConflict::InvalidKey)
    );
    Ok(())
}

#[test]
#[ignore = "provide the synthetic Python creation audit comparison"]
fn idempotent_replay_matches_python_creation_audits() -> Result<(), Box<dyn Error>> {
    use std::{
        fs::File,
        io::{BufRead, BufReader},
    };

    use serde_json::Value;

    let path = std::env::var_os("GA_TEST_EVENT_REPLAYS").ok_or("Missing synthetic audit cases")?;
    let mut count = 0;
    for line in BufReader::new(File::open(path)?).lines() {
        let (report, audit, expected): (Value, Option<Value>, String) =
            serde_json::from_str(&line?)?;
        let report = serde_json::to_vec(&report)?;
        let event = EventInput::from_json(&report, 65_536, ValidationMode::NewReport)?;
        let received = match event.verify_creation_replay(audit.as_ref()) {
            Ok(()) => "matched",
            Err(ReplayConflict::AuditUnavailable) => "unavailable",
            Err(ReplayConflict::InputChanged) => "changed",
            Err(ReplayConflict::InvalidKey) => "invalid",
        };
        count += 1;
        assert_eq!(received, expected, "Synthetic audit case {count}");
    }
    assert_eq!(count, 49);
    Ok(())
}
