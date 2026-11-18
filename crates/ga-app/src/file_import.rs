use std::{
    error::Error,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    process::ExitCode,
};

use clap::ValueEnum;
use ga_archive::storage_files::{StorageAccess, StorageFilesGuard};
use ga_db::{Database, diary::actions::FileImportOutcome};
use ga_domain::file_import::{FileMapping, ImportFileError, MAX_FILE_BYTES, MAX_MAPPING_BYTES};
use rustix::fs::{Mode as FileMode, OFlags};

use crate::configuration;

#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum Mode {
    Preview,
    Apply,
}

pub(crate) fn run(mode: Mode, file: &Path, mapping: &Path, confirmation: Option<&str>) -> ExitCode {
    let confirmation = match (mode, confirmation) {
        (Mode::Apply, Some(confirmation)) if !confirmation.is_empty() => Some(confirmation),
        (Mode::Apply, _) => {
            eprintln!("Apply requires the plan SHA-256 printed by preview");
            return ExitCode::from(2);
        }
        (Mode::Preview, _) => None,
    };
    match run_import(file, mapping, confirmation) {
        Ok(outcome) => {
            println!("{}", outcome.result);
            if outcome.invalid_preview {
                eprintln!("Preview found invalid rows; no diary entries were imported.");
                ExitCode::from(2)
            } else {
                ExitCode::SUCCESS
            }
        }
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn run_import(
    file: &Path,
    mapping: &Path,
    confirmation: Option<&str>,
) -> Result<FileImportOutcome, Box<dyn Error>> {
    let file = read_file(file, MAX_FILE_BYTES)?;
    let mapping = read_file(mapping, MAX_MAPPING_BYTES)?;
    std::str::from_utf8(&file).map_err(|_| ImportFileError::Utf8)?;
    let mapping = FileMapping::from_json(&mapping)?;
    let [url, data, tokens, locks] = configuration::read([
        "GA_DATABASE_URL",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
    ])?;
    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let locks = configuration::lock_directory(&data, &tokens, locks.map(PathBuf::from))?;
    let _files = StorageFilesGuard::acquire(&locks, StorageAccess::Active)?;
    let db = Database::connect_lazy(&url.ok_or("Storage configuration is invalid")?)
        .map_err(|_| "Storage configuration is invalid")?;
    Ok(db.run_file_import(&file, &mapping, confirmation).await?)
}

fn read_file(path: &Path, limit: usize) -> Result<Vec<u8>, Box<dyn Error>> {
    let file = rustix::fs::open(
        path,
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
        FileMode::empty(),
    )
    .map(File::from)
    .map_err(|_| "Could not read the local import file")?;
    if !file.metadata()?.is_file() {
        return Err("Import paths must be regular files".into());
    }
    let mut bytes = Vec::new();
    file.take((limit + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err(ImportFileError::Size.into());
    }
    Ok(bytes)
}
