use std::{env, error::Error, fs};

use ga_app::medication_assertion::{literal_names, name_matches, owner_assertion};
use serde_json::Value;

#[test]
#[ignore = "compares synthetic medication clause decisions from the pinned Python baseline"]
fn ownership_and_names_match_reference() -> Result<(), Box<dyn Error>> {
    let mut reference: Value = serde_json::from_slice(&fs::read(env::var(
        "GA_TEST_MEDICATION_ASSERTION_REFERENCE",
    )?)?)?;
    let clauses: Vec<(String, bool, Vec<String>)> =
        serde_json::from_value(reference["clauses"].take())?;
    let aliases: Vec<(String, String, bool)> = serde_json::from_value(reference["aliases"].take())?;
    let mut comparisons = 0;
    for (clause, owner, names) in clauses {
        assert_eq!(owner_assertion(&clause)?, owner, "ownership of {clause:?}");
        assert_eq!(
            literal_names(&clause)?.into_iter().collect::<Vec<_>>(),
            names,
            "names in {clause:?}"
        );
        comparisons += 2;
    }
    for (name, given, expected) in aliases {
        assert_eq!(
            name_matches(&name, &given)?,
            expected,
            "{name:?} / {given:?}"
        );
        comparisons += 1;
    }
    assert!(comparisons > 170);
    assert!(owner_assertion(&" ".repeat(64_001)).is_err());
    println!("{comparisons} medication ownership and name decisions matched pinned Python");
    Ok(())
}
