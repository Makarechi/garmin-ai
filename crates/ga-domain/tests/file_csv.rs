use std::{env, error::Error, fs, io, path::PathBuf};

use ga_domain::file_import::read_csv_rows;
use serde_json::{Value, json};

#[test]
#[ignore = "requires the synthetic CSV fixture generated with pinned Python"]
fn matches_import_csv_grammar_and_limits() -> Result<(), Box<dyn Error>> {
    let root = PathBuf::from(env::var("GA_TEST_FILE_CSV")?);
    let cases: Vec<Value> = serde_json::from_slice(&fs::read(root.join("cases.json"))?)?;
    for case in cases {
        let file = case["file"]
            .as_str()
            .ok_or_else(|| io::Error::other("Expected synthetic CSV filename"))?;
        let bytes = fs::read(root.join(file))?;
        let outcome = match read_csv_rows(&bytes) {
            Ok(value) => json!({"headers": value.headers, "rows": value.rows}),
            Err(error) => json!({"error": error.to_string()}),
        };
        assert_eq!(outcome, case["expected"], "{file}");
    }
    Ok(())
}
