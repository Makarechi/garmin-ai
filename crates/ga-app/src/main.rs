use std::env;
use std::ffi::OsStr;
use std::process::ExitCode;

const HELP: &str = "Garmin AI Rust migration executable

Usage: garmin-ai-rust [--help | --version]

Options:
  -h, --help     Show help
  -V, --version  Show the binary version";

fn main() -> ExitCode {
    let mut args = env::args_os().skip(1);
    let first = args.next();

    if args.next().is_some() {
        return usage_error();
    }

    match first.as_deref() {
        None => println!("{HELP}"),
        Some(arg) if arg == OsStr::new("--help") || arg == OsStr::new("-h") => println!("{HELP}"),
        Some(arg) if arg == OsStr::new("--version") || arg == OsStr::new("-V") => {
            println!("garmin-ai-rust {}", env!("CARGO_PKG_VERSION"));
        }
        Some(_) => return usage_error(),
    }

    ExitCode::SUCCESS
}

fn usage_error() -> ExitCode {
    eprintln!("Invalid arguments. Use garmin-ai-rust --help.");
    ExitCode::from(2)
}
