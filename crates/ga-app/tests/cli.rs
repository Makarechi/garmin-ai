use std::process::{Command, Output};

use serde_json::Value;

fn invoke(args: &[&str]) -> std::io::Result<Output> {
    Command::new(env!("CARGO_BIN_EXE_garmin-ai-rust"))
        .env_clear()
        .args(args)
        .output()
}

#[test]
fn help_runs_in_an_empty_environment() -> Result<(), Box<dyn std::error::Error>> {
    for args in [&[][..], &["--help"][..], &["-h"][..]] {
        let output = invoke(args)?;
        assert!(output.status.success());
        assert!(output.stderr.is_empty());
        assert!(String::from_utf8(output.stdout)?.contains("Usage: garmin-ai-rust"));
    }

    Ok(())
}

#[test]
fn exposes_frozen_commands_and_options() -> Result<(), Box<dyn std::error::Error>> {
    let inventory: Value = serde_json::from_str(include_str!(
        "../../../migration/contracts/baseline-surfaces.json"
    ))?;
    let commands = inventory["cli"]["commands"]
        .as_object()
        .ok_or("Expected frozen commands")?;
    assert_eq!(commands.len(), 20);

    for (command, contract) in commands {
        let output = invoke(&[command, "--help"])?;
        assert!(output.status.success(), "{command} --help failed");
        assert!(output.stderr.is_empty(), "{command} --help wrote to stderr");
        let help = String::from_utf8(output.stdout)?;
        assert!(help.contains(&format!("Usage: garmin-ai-rust {command}")));
        for argument in contract["arguments"]
            .as_array()
            .ok_or("Expected arguments")?
        {
            if argument["hidden"] == true {
                continue;
            }
            for flag in argument["flags"].as_array().ok_or("Expected flags")? {
                let flag = flag.as_str().ok_or("Expected flag name")?;
                assert!(
                    help.split_whitespace().any(|word| word == flag),
                    "{command} is missing {flag} from the frozen contract"
                );
            }
        }
    }
    Ok(())
}

#[test]
fn reports_the_built_version() -> Result<(), Box<dyn std::error::Error>> {
    for flag in ["--version", "-V"] {
        let output = invoke(&[flag])?;
        assert!(output.status.success());
        assert_eq!(
            String::from_utf8(output.stdout)?,
            concat!("garmin-ai-rust ", env!("CARGO_PKG_VERSION"), "\n")
        );
        assert!(output.stderr.is_empty());
    }

    Ok(())
}

#[test]
fn reports_storage_versions_without_installation_settings() -> Result<(), Box<dyn std::error::Error>>
{
    let output = invoke(&["release-info"])?;
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout)?,
        serde_json::json!({
            "application_version": env!("CARGO_PKG_VERSION"),
            "database_revision": "c54f8a72d130",
            "garmin_parser_version": 9,
            "fit_parser_version": 9,
            "fit_profile_version": "21.171",
            "archive_json": "Python-compatible UTF-8 JSON",
            "database_export_format": "garmin-ai-jsonl-v1",
            "backup_envelope": "GARMINAI1",
        })
    );
    Ok(())
}

#[test]
fn accepts_one_flag_and_keeps_argument_values_out_of_errors()
-> Result<(), Box<dyn std::error::Error>> {
    for args in [
        &["synthetic-private-value"][..],
        &["--version", "--help"][..],
    ] {
        let output = invoke(args)?;
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            String::from_utf8(output.stderr)?,
            "Invalid arguments. Use garmin-ai-rust --help.\n"
        );
    }

    Ok(())
}
