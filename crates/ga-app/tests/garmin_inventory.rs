use std::{error::Error, process::Command};

#[test]
fn inventory_matches_the_frozen_cli_without_configuration() -> Result<(), Box<dyn Error>> {
    let directory = tempfile::tempdir()?;
    let output = Command::new(env!("CARGO_BIN_EXE_garmin-ai-rust"))
        .env_clear()
        .env("GA_TIMEZONE", "invalid")
        .env("GA_DATABASE_URL", "invalid")
        .current_dir(directory.path())
        .arg("inventory")
        .output()?;

    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert_eq!(
        output.stdout,
        include_bytes!("../../../migration/contracts/garmin-legacy-inventory.tsv")
    );
    assert_eq!(directory.path().read_dir()?.count(), 0);
    Ok(())
}
