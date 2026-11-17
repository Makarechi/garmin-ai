use std::process::{Command, Output};

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
