mod authorization;
mod backup;
mod backup_space;
mod configuration;
mod extensions;
mod garmin_setup;
mod healthcheck;
mod http;
mod lifecycle;
mod mcp_stdio;
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
    /// Run the account-free synthetic dashboard
    Demo {
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 8765)]
        port: u16,
    },
    /// Sign in to Garmin from a local terminal and save the verified session
    Login {
        #[arg(long)]
        confirm_existing_owner: bool,
    },
    /// Confirm ownership of retained data using a saved Garmin session
    EnrollAccount {
        #[arg(long, required = true)]
        confirm_existing_owner: bool,
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
    /// Create a verified encrypted backup outside the source directories
    Backup { path: PathBuf },
    /// Export the logical database to a private compressed file
    Export { path: PathBuf },
    /// Recover an exported snapshot into an empty migrated database
    RestoreDb { path: PathBuf },
    /// Authenticate and extract an encrypted backup to a new directory
    UnpackBackup {
        source: PathBuf,
        destination: PathBuf,
    },
    /// Erase the local database, raw archive, and Garmin tokens
    EraseAll {
        #[arg(long)]
        confirm: String,
    },
    /// Re-enable storage after explicit local setup
    ResumeStorage,
    /// Serve MCP tools over standard input and output
    Mcp,
    /// Check an explicitly supplied source extension with synthetic data
    CheckSourceExtension {
        executable: PathBuf,
        instance_id: String,
    },
    /// Read a configured source extension and save its validated pages
    PollSourceExtension {
        executable: PathBuf,
        instance_id: String,
    },
    /// Probe the local native HTTP service or worker
    Healthcheck {
        #[arg(long, default_value_t = 8080)]
        port: u16,
        #[arg(long)]
        ready: bool,
        #[arg(long, conflicts_with_all = ["port", "ready"])]
        worker: bool,
    },
    /// Restore projections from the local Garmin archive
    Replay {
        /// Run one planning pass and process at most one due job
        #[arg(long)]
        once: bool,
    },
    /// Run archive replay and Telegram receipt processing
    Worker,
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
        Some(Command::Demo { host, port }) => {
            if http::demo::serve(&host, port).is_err() {
                eprintln!("Could not run the demo server.");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::Login {
            confirm_existing_owner,
        }) => {
            if let Err(error) = garmin_setup::login(confirm_existing_owner) {
                eprintln!("{error}");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::EnrollAccount { .. }) => {
            if let Err(error) = garmin_setup::enroll() {
                eprintln!("{error}");
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
        Some(Command::Backup { path }) => {
            return report(backup::create(&path));
        }
        Some(Command::Export { path }) => {
            return report(backup::export(&path));
        }
        Some(Command::RestoreDb { path }) => {
            return report(backup::restore(&path));
        }
        Some(Command::UnpackBackup {
            source,
            destination,
        }) => {
            return report(backup::unpack(&source, &destination));
        }
        Some(Command::EraseAll { confirm }) => return report(lifecycle::erase(&confirm)),
        Some(Command::ResumeStorage) => return report(lifecycle::resume()),
        Some(Command::Mcp) => {
            if let Err(error) = mcp_stdio::run() {
                eprintln!("{error}");
                return ExitCode::FAILURE;
            }
        }
        Some(Command::CheckSourceExtension {
            executable,
            instance_id,
        }) => {
            return report(extensions::check_source(&executable, &instance_id));
        }
        Some(Command::PollSourceExtension {
            executable,
            instance_id,
        }) => {
            return report(extensions::poll_source(&executable, &instance_id));
        }
        Some(Command::Healthcheck {
            port,
            ready,
            worker,
        }) => {
            let healthy = if worker {
                healthcheck::worker()
            } else {
                healthcheck::probe(port, ready).unwrap_or(false)
            };
            if !healthy {
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

fn report(result: Result<(), Box<dyn std::error::Error>>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}

fn usage_error() -> ExitCode {
    eprintln!("Invalid arguments. Use garmin-ai-rust --help.");
    ExitCode::from(2)
}
