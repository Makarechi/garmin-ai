use std::{env, error::Error, fs, io, path::PathBuf};

use ga_domain::{
    file_import::{ImportFileError, read_json_rows},
    sha256_hex,
};
use serde_json::{Value, json};

#[test]
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
fn accepts_deep_file_rows_in_the_linux_amd64_reference_range() -> Result<(), Box<dyn Error>> {
    for (opening, closing, accepted, rejected) in [
        ("[", "]", 52_000, 52_400),
        ("{\"v\":", "}", 30_600, 31_000),
        ("[{\"v\":", "}]", 19_300, 19_500),
    ] {
        let input = |depth| {
            format!(
                "[{{\"val\":{}0{}}}]",
                opening.repeat(depth),
                closing.repeat(depth)
            )
        };
        let valid = input(accepted);
        let rows = read_json_rows(valid.as_bytes())?;
        let [Ok(row)] = rows.as_slice() else {
            return Err(io::Error::other("Expected one valid object row").into());
        };
        assert_eq!(row.canonical_json()?, valid[1..valid.len() - 1]);
        assert!(matches!(
            read_json_rows(input(rejected).as_bytes()),
            Err(ImportFileError::InvalidFormat)
        ));
    }
    Ok(())
}

#[test]
fn file_errors_follow_the_reference_order_within_an_object() {
    for (input, expected) in [
        (r#"[{"a":0,"a":1,"z":}]"#, ImportFileError::InvalidFormat),
        (r#"[{"a":0,"a":1,"z":NaN}]"#, ImportFileError::JsonNonfinite),
        (
            r#"[{"a":0,"a":1,"z":1e-1000}]"#,
            ImportFileError::JsonPrecision,
        ),
        (
            r#"[{"nested":{"a":0,"a":1},"z":NaN}]"#,
            ImportFileError::JsonDuplicateKey,
        ),
    ] {
        assert_eq!(read_json_rows(input.as_bytes()).err(), Some(expected));
    }
}

#[test]
#[ignore = "requires the synthetic JSON fixture generated with pinned Python"]
fn matches_json_syntax_precision_and_redacted_rows() -> Result<(), Box<dyn Error>> {
    let root = PathBuf::from(env::var("GA_TEST_FILE_JSON")?);
    let cases: Vec<Value> = serde_json::from_slice(&fs::read(root.join("cases.json"))?)?;
    for case in cases {
        let file = case["file"]
            .as_str()
            .ok_or_else(|| io::Error::other("Expected synthetic JSON filename"))?;
        let bytes = fs::read(root.join(file))?;
        let outcome = match read_json_rows(&bytes) {
            Ok(rows) => {
                let rows: Vec<_> = rows
                    .into_iter()
                    .map(|row| {
                        Ok(match row {
                            Err(issue) => json!({"issue": issue.code()}),
                            Ok(row)
                                if file.starts_with("deep") || file.starts_with("canonical-") =>
                            {
                                let document = row.canonical_json()?;
                                json!({"canonical_sha256": sha256_hex(document.as_bytes())})
                            }
                            Ok(_) if matches!(file, "dense.json" | "largest-string.json") => {
                                json!({"valid": true})
                            }
                            Ok(row) => json!({"row": row.fields()}),
                        })
                    })
                    .collect::<Result<_, ImportFileError>>()?;
                json!({"rows": rows})
            }
            Err(error) => json!({"error": error.to_string()}),
        };
        assert_eq!(outcome, case["expected"], "{file}");
    }
    Ok(())
}
