mod authorization;
mod http;

use std::{env, ffi::OsStr, process::ExitCode};

use clap::{CommandFactory, Parser, Subcommand, error::ErrorKind};

#[derive(Parser)]
#[command(
    name = "garmin-ai-rust",
    version,
    about = "Garmin AI Rust migration executable"
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Serve the local liveness endpoint
    Serve {
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 8080)]
        port: u16,
    },
}

fn main() -> ExitCode {
    let arguments: Vec<_> = env::args_os().collect();
    let standalone_flags = ["--help", "-h", "--version", "-V"].map(OsStr::new);
    if arguments.len() > 2 && standalone_flags.contains(&arguments[1].as_os_str()) {
        return usage_error();
    }
    let cli = match Cli::try_parse_from(arguments) {
        Ok(cli) => cli,
        Err(error)
            if matches!(
                error.kind(),
                ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
            ) =>
        {
            return if error.print().is_ok() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            };
        }
        Err(_) => return usage_error(),
    };

    match cli.command {
        None => {
            if Cli::command().print_help().is_err() {
                return ExitCode::FAILURE;
            }
            println!();
        }
        Some(Command::Serve { host, port }) => {
            if http::serve(&host, port).is_err() {
                eprintln!("Could not run the HTTP server.");
                return ExitCode::FAILURE;
            }
        }
    }

    ExitCode::SUCCESS
}

fn usage_error() -> ExitCode {
    eprintln!("Invalid arguments. Use garmin-ai-rust --help.");
    ExitCode::from(2)
}
