use std::{env, error::Error, fs, io, path::PathBuf};

use ga_domain::{file_import::read_json_rows, sha256_hex};
use serde_json::{Value, json};

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
                            Ok(row) if file.starts_with("deep") => {
                                let document = row.canonical_json()?;
                                json!({"canonical_sha256": sha256_hex(document.as_bytes())})
                            }
                            Ok(_) if matches!(file, "dense.json" | "largest-string.json") => {
                                json!({"valid": true})
                            }
                            Ok(row) => json!({"row": row.fields()}),
                        })
                    })
                    .collect::<Result<_, ga_domain::file_import::ImportFileError>>()?;
                json!({"rows": rows})
            }
            Err(error) => json!({"error": error.to_string()}),
        };
        assert_eq!(outcome, case["expected"], "{file}");
    }
    Ok(())
}
