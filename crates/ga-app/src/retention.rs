use std::{error::Error, path::PathBuf};

use ga_archive::storage_files::{StorageAccess, StorageFilesGuard};
use ga_db::{Database, retention::RetentionSelection};
use ga_domain::timestamp::StoredInstant;

use crate::configuration;

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run(selection: RetentionSelection<'_>) -> Result<(), Box<dyn Error>> {
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
    let database = Database::connect_lazy(&url.ok_or("Storage configuration is invalid")?)
        .map_err(|_| "Storage configuration is invalid")?;
    let micros = time::OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1000);
    let result = database
        .prune_telegram_text(
            StoredInstant::from_unix_microseconds(i64::try_from(micros)?)?,
            selection,
        )
        .await?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
