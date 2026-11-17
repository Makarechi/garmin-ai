use std::{
    error::Error,
    fs::File,
    io::{BufRead, BufReader},
    path::PathBuf,
};

use ga_fit::{FitError, MAX_FIT_BYTES, visit_fit_projection};
use serde_json::{Value, json};

const SIMPLE: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";

#[test]
fn publishes_candidate_fields_in_the_legacy_json_shape() -> Result<(), FitError> {
    let inventory = visit_fit_projection(
        SIMPLE,
        MAX_FIT_BYTES,
        || false,
        |row| {
            assert_eq!(row.name, "record");
            assert_eq!(row.frame_index, 0);
            assert_eq!(row.message_index, 0);
            assert_eq!(row.byte_offset, 23);
            assert_eq!(
                row.payload,
                json!({
                    "heart_rate": 72,
                    "_fit": {
                        "message_index": 0,
                        "global_message_number": 20,
                        "local_message_number": 0,
                        "developer_data": false,
                        "fields": [{
                            "name": "heart_rate",
                            "field_number": 3,
                            "type": "uint8",
                            "value": 72,
                            "raw_value": 72,
                            "units": "bpm",
                            "developer": false,
                            "developer_data_index": null,
                            "native_field_number": null
                        }]
                    }
                })
            );
            Ok(())
        },
    )?;
    assert_eq!(inventory.messages, 1);
    Ok(())
}

#[test]
#[ignore = "uses Python's synthetic ActivityPart output from migration/check_fit_projection.py"]
fn projection_matches_the_pinned_python_records() -> Result<(), Box<dyn Error>> {
    let base = PathBuf::from(
        std::env::var_os("GA_TEST_FIT_PROJECTION").ok_or("fixture directory missing")?,
    );
    let manifest: Vec<(String, u32)> =
        serde_json::from_reader(File::open(base.join("manifest.json"))?)?;
    for (name, count) in manifest {
        let fit = base.join(&name);
        let mut expected = BufReader::new(File::open(fit.with_extension("ndjson"))?).lines();
        let result = visit_fit_projection(
            BufReader::with_capacity(64 * 1024, File::open(&fit)?),
            MAX_FIT_BYTES,
            || false,
            |row| {
                let expected: (String, Value) = serde_json::from_str(
                    &expected
                        .next()
                        .ok_or(FitError::InvalidRecord)?
                        .map_err(FitError::from)?,
                )
                .map_err(|_| FitError::InvalidRecord)?;
                assert_eq!(
                    (row.name, row.payload),
                    expected,
                    "{name} record {}",
                    row.message_index
                );
                Ok(())
            },
        )?;
        assert_eq!(result.messages, count, "{name}");
        assert!(expected.next().is_none(), "{name}");
    }
    let rejections: Vec<(String, String)> =
        serde_json::from_reader(File::open(base.join("rejections.json"))?)?;
    for (name, expected) in rejections {
        let file = File::open(base.join(&name))?;
        let error = match expected.as_str() {
            "AssertionError" => FitError::MissingHeartRateStart,
            "TypeError" => FitError::InvalidHeartRateComponent,
            _ => return Err("Unexpected fixture error type".into()),
        };
        assert_eq!(
            visit_fit_projection(file, MAX_FIT_BYTES, || false, |_| Ok(())),
            Err(error),
            "{name}"
        );
    }
    Ok(())
}

#[test]
#[ignore = "uses the generated synthetic R14 100 MiB FIT fixture"]
fn projected_fields_remain_streaming_at_the_export_ceiling() -> Result<(), Box<dyn Error>> {
    let source = File::open(std::env::var_os("GA_TEST_MAX_FIT").ok_or("fixture missing")?)?;
    assert_eq!(source.metadata()?.len(), MAX_FIT_BYTES);
    let result = visit_fit_projection(
        BufReader::with_capacity(64 * 1024, source),
        MAX_FIT_BYTES,
        || false,
        |row| {
            assert_eq!(row.name, "record");
            let fields = row.payload["_fit"]["fields"]
                .as_array()
                .ok_or(FitError::InvalidRecord)?;
            assert_eq!(fields.len(), 2);
            assert_eq!(fields[0]["field_number"], 108);
            assert_eq!(fields[0]["raw_value"], 0);
            assert_eq!(fields[1]["field_number"], 99);
            if row.message_index < 409_599 {
                assert_eq!(fields[1]["raw_value"].as_array().map(Vec::len), Some(255));
            }
            Ok(())
        },
    )?;
    assert_eq!(result.messages, 409_600);
    assert_eq!(result.member.bytes, MAX_FIT_BYTES);
    Ok(())
}
