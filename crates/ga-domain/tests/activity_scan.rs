use std::{env, error::Error, fs};

use ga_domain::activity_scan::{ActivityScanAdvance, ActivityScanPage, advance_activity_scan};
use serde_json::{Value, json};
use time::{Date, format_description::well_known::Iso8601};

#[test]
#[ignore = "requires mutable activity pages from the pinned Python fixture"]
fn matches_the_original_activity_scan_transitions() -> Result<(), Box<dyn Error>> {
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_ACTIVITY_SCAN")?)?)?;
    let zone = fixture["timezone"].as_str().ok_or("Expected timezone")?;
    let now = fixture["now"].as_str().ok_or("Expected clock")?.parse()?;
    for (index, case) in fixture["cases"]
        .as_array()
        .ok_or("Expected cases")?
        .iter()
        .enumerate()
    {
        let cursor = &case["cursor"];
        let since = cursor["since"].as_str().ok_or("Expected a date")?;
        let anchor: Vec<String> = serde_json::from_value(cursor["anchor"].clone())?;
        let input = ActivityScanPage {
            offset: u32::try_from(cursor["offset"].as_u64().ok_or("Expected offset")?)?,
            round: u8::try_from(cursor["round"].as_u64().ok_or("Expected round")?)?,
            anchor: &anchor,
            since: Date::parse(since, &Iso8601::DEFAULT)?,
            backfill: cursor["backfill"]
                .as_bool()
                .ok_or("Expected backfill flag")?,
        };
        let values = case["values"].as_array().ok_or("Expected a page")?;
        let actual = match advance_activity_scan(input, values, zone, now) {
            Ok(ActivityScanAdvance::Following {
                offset,
                round,
                anchor,
            }) => json!({"status":"running", "offset":offset, "round":round, "anchor":anchor}),
            Ok(ActivityScanAdvance::Complete { next_scan_at }) => {
                json!({"status":"complete", "next_scan_at":next_scan_at.to_string()})
            }
            Ok(ActivityScanAdvance::Unstable { next_scan_at }) => {
                json!({"status":"unstable_inventory", "next_scan_at":next_scan_at.to_string()})
            }
            Err(_) => json!({"error":true}),
        };
        assert_eq!(actual, case["expected"], "case {index}");
    }
    Ok(())
}
