use std::{
    error::Error,
    fs,
    io::{self, BufReader},
    path::{Path, PathBuf},
};

use base64::{Engine, engine::general_purpose::URL_SAFE};
use flate2::{Compression, bufread::MultiGzDecoder, write::GzEncoder};
use ga_archive::{
    backup_bundle::{BackupBundle, write_backup_bundle_guarded},
    backup_envelope::{open_source, write_private_export},
    backup_space::{ExportSize, estimate_backup_space},
    backup_unpack::unpack_backup,
    backup_workspace::BackupWorkspace,
    storage_files::{StorageAccess, StorageFilesGuard, validate_backup_layout},
};
use ga_db::Database;
use ga_domain::timestamp::StoredInstant;
use tokio::runtime::{Builder, Runtime};

use crate::configuration;

type CommandResult = Result<(), Box<dyn Error>>;
const BUFFER: usize = 64 * 1024;

struct BackupSettings {
    url: Option<String>,
    data: PathBuf,
    tokens: PathBuf,
    locks: PathBuf,
    key: Option<String>,
}

impl BackupSettings {
    fn read() -> Result<Self, Box<dyn Error>> {
        let [url, data, tokens, locks, key, backups] = configuration::read([
            "GA_DATABASE_URL",
            "GA_DATA_DIR",
            "GA_TOKEN_DIR",
            "GA_LOCK_DIR",
            "GA_BACKUP_KEY",
            "GA_BACKUP_DIR",
        ])?;
        let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
        let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
        let locks = configuration::lock_directory(&data, &tokens, locks.map(PathBuf::from))?;
        validate_backup_layout(
            &data,
            &tokens,
            &locks,
            Path::new(backups.as_deref().unwrap_or("backups")),
        )?;
        Ok(Self {
            url,
            data,
            tokens,
            locks,
            key,
        })
    }

    fn database(&self) -> Result<Database, Box<dyn Error>> {
        let address = self
            .url
            .as_deref()
            .ok_or("Backup storage configuration is invalid")?;
        Database::connect_lazy(address)
            .map_err(|_| "Backup storage configuration is invalid".into())
    }

    fn key(&self) -> Result<[u8; 32], Box<dyn Error>> {
        let input = self
            .key
            .as_deref()
            .ok_or("GA_BACKUP_KEY must encode 32 random bytes")?;
        URL_SAFE
            .decode(input)
            .ok()
            .and_then(|decoded| decoded.try_into().ok())
            .ok_or_else(|| "GA_BACKUP_KEY must encode 32 random bytes".into())
    }

    fn require_capacity(&self, destination: &Path, estimate: ExportSize) -> CommandResult {
        if !estimate_backup_space(&self.data, &self.tokens, destination, estimate)?.ready() {
            return Err("Insufficient storage space for an encrypted backup".into());
        }
        Ok(())
    }
}

pub(crate) fn create(destination: &Path) -> CommandResult {
    let settings = BackupSettings::read()?;
    let key = settings.key()?;
    let _files = StorageFilesGuard::acquire(&settings.locks, StorageAccess::Active)?;
    let runtime = runtime()?;
    let database = runtime.block_on(async { settings.database() })?;
    let mut sources = runtime.block_on(database.acquire_backup_sources())?;
    let size = runtime
        .block_on(database.backup_database_bytes())
        .map_err(|_| "Backup database size is unavailable")?;
    settings.require_capacity(destination, ExportSize::DatabaseBytes(size))?;

    let workspace = BackupWorkspace::acquire(&settings.data)?;
    let export = workspace.export_path();
    let at = current_time()?;
    let snapshot = write_private_export(export, |file| {
        let mut compressed = GzEncoder::new(file, Compression::best());
        let snapshot = runtime
            .block_on(sources.export(at, &mut compressed))
            .map_err(io::Error::other)?;
        compressed.finish()?;
        Ok(snapshot)
    })?;
    let staged_bytes = fs::metadata(export)?.len();
    settings.require_capacity(destination, ExportSize::AlreadyStaged(staged_bytes))?;
    write_backup_bundle_guarded(
        destination,
        &key,
        BackupBundle {
            data_dir: &settings.data,
            token_dir: &settings.tokens,
            database_export: export,
            required_raw: &snapshot.required_raw,
        },
        || runtime.block_on(sources.check()).map_err(io::Error::other),
    )?;
    println!("{}", serde_json::to_string(&snapshot.counts)?);
    Ok(())
}

pub(crate) fn export(destination: &Path) -> CommandResult {
    let settings = BackupSettings::read()?;
    let runtime = runtime()?;
    let database = runtime.block_on(async { settings.database() })?;
    let owner = runtime.block_on(database.enrolled_owner_id())?;
    let at = current_time()?;
    let counts = write_private_export(destination, |file| {
        let mut compressed = GzEncoder::new(file, Compression::best());
        let counts = runtime
            .block_on(database.export_database_rows(owner, at, &mut compressed))
            .map_err(io::Error::other)?;
        compressed.finish()?;
        Ok(counts)
    })?;
    println!("{}", serde_json::to_string(&counts)?);
    Ok(())
}

pub(crate) fn restore(source: &Path) -> CommandResult {
    let settings = BackupSettings::read()?;
    let files = StorageFilesGuard::acquire(&settings.locks, StorageAccess::Recovery)?;
    let runtime = runtime()?;
    let database = runtime.block_on(async { settings.database() })?;
    let file = open_source(source)?;
    let compressed = MultiGzDecoder::new(BufReader::with_capacity(BUFFER, file));
    let mut input = BufReader::with_capacity(BUFFER, compressed);
    let mut activation = None;
    let counts = runtime.block_on(database.restore_current_database_with_activation(
        &mut input,
        || {
            activation = Some(files.begin_activation().map_err(io::Error::other)?);
            Ok(())
        },
    ))?;
    activation
        .ok_or("The recovery fence was not established")?
        .complete()?;
    println!("{}", serde_json::to_string(&counts)?);
    Ok(())
}

pub(crate) fn unpack(source: &Path, destination: &Path) -> CommandResult {
    let settings = BackupSettings::read()?;
    unpack_backup(
        source,
        destination,
        &settings.key()?,
        &settings.data,
        &settings.tokens,
    )?;
    println!("Backup unpacked.");
    Ok(())
}

fn runtime() -> io::Result<Runtime> {
    Builder::new_current_thread().enable_all().build()
}

fn current_time() -> Result<StoredInstant, Box<dyn Error>> {
    let now = time::OffsetDateTime::now_utc()
        .unix_timestamp_nanos()
        .div_euclid(1000);
    Ok(StoredInstant::from_unix_microseconds(i64::try_from(now)?)?)
}
