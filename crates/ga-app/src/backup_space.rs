use std::path::{Path, PathBuf};

use ga_archive::backup_space::{ExportSize, RESERVE_BYTES, estimate_backup_space};
use ga_db::Database;
use serde_json::{Value, json};

#[tokio::main(flavor = "current_thread")]
pub async fn run(destination: &Path) -> Result<(), &'static str> {
    let [url, data, tokens] =
        crate::configuration::read(["GA_DATABASE_URL", "GA_DATA_DIR", "GA_TOKEN_DIR"])?;
    let database = Database::connect_lazy(&url.ok_or("Backup storage configuration is invalid")?)
        .map_err(|_| "Backup storage configuration is invalid")?;
    let bytes = database
        .backup_database_bytes()
        .await
        .map_err(|_| "Backup database size is unavailable")?;
    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let result = estimate_backup_space(
        &data,
        &tokens,
        destination,
        ExportSize::DatabaseBytes(bytes),
    )
    .map_err(|_| "Backup storage capacity could not be estimated")?;
    let volumes:Vec<Value>=result.volumes.iter().map(|volume|json!({
        "role":volume.role.label(),"free_bytes":volume.free_bytes,"required_bytes":volume.required_bytes
    })).collect();
    println!(
        "{}",
        json!({
            "status":if result.ready(){"ready"}else{"insufficient"},
            "estimate_only":true,"reserve_bytes":RESERVE_BYTES,"volumes":volumes,
        })
    );
    Ok(())
}
