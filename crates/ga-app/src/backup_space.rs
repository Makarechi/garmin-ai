use std::path::{Path, PathBuf};

use ga_archive::backup_space::{BackupSpace, ExportSize, estimate_backup_space};
use ga_db::{Database, jobs::backup_capacity_report};

#[tokio::main(flavor = "current_thread")]
pub async fn run(destination: &Path) -> Result<(), &'static str> {
    let [url, data, tokens] =
        crate::configuration::read(["GA_DATABASE_URL", "GA_DATA_DIR", "GA_TOKEN_DIR"])?;
    let database = Database::connect_lazy(&url.ok_or("Backup storage configuration is invalid")?)
        .map_err(|_| "Backup storage configuration is invalid")?;
    let (data, tokens) = paths(data, tokens);
    let result = estimate(&database, data, tokens, destination.to_owned()).await?;
    println!("{}", backup_capacity_report(&result));
    Ok(())
}

pub(crate) fn paths(data: Option<String>, tokens: Option<String>) -> (PathBuf, PathBuf) {
    (
        PathBuf::from(data.unwrap_or_else(|| "data".into())),
        PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into())),
    )
}

pub(crate) async fn estimate(
    database: &Database,
    data: PathBuf,
    tokens: PathBuf,
    destination: PathBuf,
) -> Result<BackupSpace, &'static str> {
    let bytes = database
        .backup_database_bytes()
        .await
        .map_err(|_| "Backup database size is unavailable")?;
    let unavailable = "Backup storage capacity could not be estimated";
    tokio::task::spawn_blocking(move || {
        estimate_backup_space(
            &data,
            &tokens,
            &destination,
            ExportSize::DatabaseBytes(bytes),
        )
        .map_err(|_| unavailable)
    })
    .await
    .map_err(|_| unavailable)?
}
