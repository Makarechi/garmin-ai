use std::{error::Error, path::PathBuf};

use ga_archive::{
    erasure_files::ErasureFiles,
    storage_files::{StorageAccess, StorageFilesGuard, validate_backup_layout},
};
use ga_db::Database;
use serde_json::json;

use crate::configuration;

type CommandResult = Result<(), Box<dyn Error>>;

pub(crate) fn erase(confirmation: &str) -> CommandResult {
    if confirmation != "ERASE ALL LOCAL HEALTH DATA" {
        return Err("Exact erasure confirmation required".into());
    }
    run(Operation::Erase)
}

pub(crate) fn resume() -> CommandResult {
    run(Operation::Resume)
}

enum Operation {
    Erase,
    Resume,
}

#[tokio::main(flavor = "current_thread")]
async fn run(operation: Operation) -> CommandResult {
    let [url, data, tokens, locks, backups] = configuration::read([
        "GA_DATABASE_URL",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
        "GA_BACKUP_DIR",
    ])?;
    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let backups = PathBuf::from(backups.unwrap_or_else(|| "backups".into()));
    let locks = configuration::lock_directory(&data, &tokens, locks.map(PathBuf::from))?;
    validate_backup_layout(&data, &tokens, &locks, &backups)?;
    let local = StorageFilesGuard::acquire(&locks, StorageAccess::Recovery)?;
    let database = Database::connect_lazy(&url.ok_or("Storage configuration is invalid")?)
        .map_err(|_| "Storage configuration is invalid")?;
    let mut maintenance = database.acquire_storage_maintenance().await?;
    match operation {
        Operation::Erase => {
            let files = ErasureFiles::prepare(&data, &tokens, &locks, &backups)?;
            let erasure = local.begin_erasure()?;
            maintenance.erase_database().await?;
            files.remove()?;
            erasure.complete()?;
            maintenance.release().await?;
            println!(
                "{}",
                json!({
                    "erased":true,
                    "note":"Separately stored backups and provider-side copies are not affected.",
                })
            );
        }
        Operation::Resume => {
            let activation = local.begin_activation()?;
            activation.complete()?;
            maintenance.resume_database().await?;
            maintenance.release().await?;
            println!("Storage re-enabled.");
        }
    }
    Ok(())
}
