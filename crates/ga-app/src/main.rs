mod authorization;
mod backup_space;
mod configuration;
mod http;
mod migrate;
mod replay_worker;
mod retention;

use std::{env, ffi::OsStr, path::PathBuf, process::ExitCode};

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
    /// Estimate local staging and destination capacity for an encrypted backup
    BackupSpace { path: PathBuf },
    /// Preview or redact old completed transport text while retaining replay records
    PruneTelegramText {
        #[arg(long, default_value_t = 90)]
        older_than_days: u16,
        #[arg(long, default_value_t = 1000)]
        limit: u16,
        #[arg(long)]
        apply: bool,
        #[arg(long)]
        cursor: Option<String>,
        #[arg(long)]
        answer_cursor: Option<String>,
        #[arg(long)]
        neutral_cursor: Option<String>,
    },
    /// Inspect or upgrade the local PostgreSQL schema
    Migrate {
        #[arg(long)]
        status: bool,
    },
    /// Run archive replay and Telegram receipt processing
    Worker,
    /// Restore projections from the local Garmin archive
    Replay {
        /// Run one planning pass and process at most one due job
        #[arg(long)]
        once: bool,
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
        Some(Command::BackupSpace { path }) => {
            if let Err(error) = backup_space::run(&path) {
                eprintln!("{error}");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::PruneTelegramText {
            older_than_days,
            limit,
            apply,
            cursor,
            answer_cursor,
            neutral_cursor,
        }) => {
            let selection = ga_db::retention::RetentionSelection {
                older_than_days,
                limit,
                apply,
                cursor: cursor.as_deref(),
                answer_cursor: answer_cursor.as_deref(),
                neutral_cursor: neutral_cursor.as_deref(),
            };
            if let Err(error) = retention::run(selection) {
                eprintln!("{error}");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::Migrate { status }) => {
            if let Err(error) = migrate::run(status) {
                eprintln!("{error}");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::Replay { once }) => {
            if let Err(error) = replay_worker::run(once, false) {
                eprintln!("{error}");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::Worker) => {
            if let Err(error) = replay_worker::run(false, true) {
                eprintln!("{error}");
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
