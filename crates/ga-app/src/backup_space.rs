use std::{
    env, io,
    path::{Path, PathBuf},
};

use ga_archive::backup_space::{ExportSize, RESERVE_BYTES, estimate_backup_space};
use ga_db::Database;
use serde_json::{Value, json};

#[tokio::main(flavor = "current_thread")]
pub async fn run(destination: &Path) -> Result<(), &'static str> {
    let [url, data, tokens] = configuration()?;
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

fn configuration() -> Result<[Option<String>; 3], &'static str> {
    const KEYS: [&str; 3] = ["GA_DATABASE_URL", "GA_DATA_DIR", "GA_TOKEN_DIR"];
    const ERROR: &str = "Backup storage configuration is invalid";
    let mut values = [None, None, None];
    match dotenvy::from_path_iter(".env") {
        Ok(entries) => {
            for (name, value) in entries.filter_map(Result::ok) {
                if let Some(index) = KEYS.iter().position(|key| name.eq_ignore_ascii_case(key)) {
                    values[index] = Some(value);
                }
            }
        }
        Err(dotenvy::Error::Io(error)) if error.kind() == io::ErrorKind::NotFound => {}
        Err(_) => return Err(ERROR),
    }
    for (name, value) in env::vars_os() {
        if let Some(index) = name
            .to_str()
            .and_then(|name| KEYS.iter().position(|key| name.eq_ignore_ascii_case(key)))
        {
            values[index] = Some(value.into_string().map_err(|_| ERROR)?);
        }
    }
    Ok(values)
}
