use std::{error::Error, path::PathBuf};

use ga_archive::storage_files::{StorageAccess, StorageFilesGuard};
use ga_db::Database;
use serde_json::json;

use crate::configuration;

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run(status: bool) -> Result<(), Box<dyn Error>> {
    let [url, data, tokens, locks] = configuration::read([
        "GA_DATABASE_URL",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
    ])?;
    let database = Database::connect_lazy(&url.ok_or("Storage configuration is invalid")?)
        .map_err(|_| "Storage configuration is invalid")?;
    if status {
        let state = database.schema_status().await?;
        println!(
            "{}",
            json!({"revision":state.revision(),"current":state==ga_db::schema_migration::SchemaState::Current})
        );
        return Ok(());
    }
    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let locks = configuration::lock_directory(&data, &tokens, locks.map(PathBuf::from))?;
    let _files = StorageFilesGuard::acquire(&locks, StorageAccess::Recovery)?;
    database.migrate_and_initialize().await?;
    println!("Database schema upgraded.");
    Ok(())
}
