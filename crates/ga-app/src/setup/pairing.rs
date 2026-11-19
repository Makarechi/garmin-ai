use std::{
    env,
    error::Error,
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    time::Duration,
};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ga_app::{telegram_settings::TelegramIngressSettings, telegram_transport::TelegramTransport};
use ga_archive::storage_files::{StorageAccess, StorageFilesGuard};
use ga_db::Database;
use rustix::fs::{FlockOperation, flock};
use serde_json::Value;
use subtle::ConstantTimeEq;

use super::{SetupFile, publish_settings, random};
use crate::configuration;

const ENV_FILE: &str = "Pairing requires an existing regular environment file";
const OWNER_SETTING: &str = "GA_TELEGRAM_USER_ID";
const OWNER_ERROR: &str = "Telegram owner must be a valid positive ID";
const CHANGED: &str = "Environment file changed during pairing; restart pairing";
const EXPIRED: &str = "Pairing expired without a matching private message";

struct Settings {
    owner: i64,
    token: String,
    url: String,
    locks: PathBuf,
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn pair_telegram(
    path: &Path,
    container_runtime: bool,
) -> Result<(), Box<dyn Error>> {
    let path = env::current_dir()?.join(path);
    let original = read_env_file(&path)?;
    let file = SetupFile::parse(&original)?;
    let settings = Settings::read(&original, &path, container_runtime)?;
    let _files = StorageFilesGuard::acquire(&settings.locks, StorageAccess::Active)?;
    let database =
        Database::connect_lazy(&settings.url).map_err(|_| "Pairing database is invalid")?;
    let mut lease = database.acquire_runtime_lease().await?;
    if settings.owner > 0 {
        lease
            .reserve_telegram_owner(settings.owner, || {
                (read_env_file(&path)? == original)
                    .then_some(())
                    .ok_or(CHANGED)
            })
            .await?;
    } else {
        lease.require_unpaired_telegram().await?;
        let bot = TelegramTransport::new(&settings.token)?;
        let owner = lease.while_held(discover_owner(&bot)).await??;
        lease
            .reserve_telegram_owner(owner, || publish_owner(&path, &original, &file, owner))
            .await?;
    }
    println!(
        "Telegram owner paired. Recreate the Compose API and worker with the updated environment."
    );
    Ok(())
}

impl Settings {
    fn read(contents: &str, path: &Path, container: bool) -> Result<Self, &'static str> {
        let [
            owner,
            token,
            integrations,
            database,
            data,
            tokens,
            backups,
            locks,
        ] = configuration::file_values(
            dotenvy::from_read_iter(contents.as_bytes()),
            [
                OWNER_SETTING,
                "GA_TELEGRAM_BOT_TOKEN",
                "GA_INTEGRATIONS",
                "GA_DATABASE_URL",
                "GA_DATA_DIR",
                "GA_TOKEN_DIR",
                "GA_BACKUP_DIR",
                "GA_LOCK_DIR",
            ],
        );
        let owner = owner
            .as_deref()
            .unwrap_or("0")
            .trim()
            .parse::<i64>()
            .ok()
            .filter(|owner| (0..(1_i64 << 52)).contains(owner))
            .ok_or(OWNER_ERROR)?;
        let token = token
            .filter(|token| !token.is_empty() && !token.starts_with("replace-with-"))
            .ok_or("Configure the Telegram bot token locally before pairing")?;
        // Ask the worker's selector which instance will be active after pairing.
        let selected = TelegramIngressSettings::selected(Some("1"), integrations.as_deref())
            .map_err(
                |_| "GA_INTEGRATIONS must contain a valid Telegram configuration before pairing",
            )?;
        if selected.is_none_or(|settings| settings.instance_id() != "primary") {
            return Err("GA_INTEGRATIONS must enable channel:telegram:primary before pairing");
        }
        let parent = path.parent().ok_or(ENV_FILE)?;
        let external = |name| {
            env::var(name)
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or("Container pairing requires the worker's database and storage configuration")
        };
        let (url, locks) = if container {
            let url = external("GA_DATABASE_URL")?;
            let data = PathBuf::from(external("GA_DATA_DIR")?);
            let tokens = PathBuf::from(external("GA_TOKEN_DIR")?);
            let locks = PathBuf::from(external("GA_LOCK_DIR")?);
            let backups = PathBuf::from(external("GA_BACKUP_DIR")?);
            if [&data, &tokens, &locks, &backups]
                .iter()
                .any(|path| !path.is_absolute())
            {
                return Err("Container pairing requires absolute worker storage directories");
            }
            ga_archive::storage_files::validate_backup_layout(&data, &tokens, &locks, &backups)
                .map_err(|_| "Worker storage directories are invalid")?;
            (url, locks)
        } else {
            let storage = |value: Option<String>, default: &str| {
                parent.join(
                    value
                        .as_deref()
                        .filter(|value| !value.is_empty())
                        .unwrap_or(default),
                )
            };
            let data = storage(data, "data");
            let tokens = storage(tokens, "tokens/garmin");
            let backups = storage(backups, "backups");
            let locks = configuration::lock_directory(
                &data,
                &tokens,
                locks
                    .filter(|value| !value.is_empty())
                    .map(|value| parent.join(value)),
            )?;
            ga_archive::storage_files::validate_backup_layout(&data, &tokens, &locks, &backups)
                .map_err(|_| "Pairing storage directories are invalid")?;
            (
                database
                    .filter(|value| !value.is_empty())
                    .ok_or("Pairing requires a configured database")?,
                locks,
            )
        };
        Ok(Self {
            owner,
            token,
            url,
            locks,
        })
    }
}

async fn discover_owner(bot: &TelegramTransport) -> Result<i64, Box<dyn Error>> {
    let name = bot
        .pairing_bot()
        .await?
        .ok_or("Pairing requires an unconfigured bot without a webhook")?;
    let command = format!("/pair {}", URL_SAFE_NO_PAD.encode(random::<24>()?));
    println!("Open a private chat with @{name} and send within 3 minutes: {command}");
    io::stdout().flush()?;
    tokio::time::timeout(Duration::from_secs(180), matching_update(bot, &command))
        .await
        .map_err(|_| EXPIRED)?
}

async fn matching_update(bot: &TelegramTransport, command: &str) -> Result<i64, Box<dyn Error>> {
    let mut offset = None;
    loop {
        for update in bot.pairing_updates(offset, true).await? {
            let following = update
                .id
                .checked_add(1)
                .ok_or("Invalid Telegram pairing update")?;
            offset = Some(offset.map_or(following, |known: i64| known.max(following)));
            if let Some(owner) = private_owner(&update.body["message"], command) {
                bot.pairing_updates(Some(following), false).await?;
                return Ok(owner);
            }
        }
    }
}

fn private_owner(message: &Value, command: &str) -> Option<i64> {
    let owner = message["from"]["id"]
        .as_i64()
        .filter(|id| (1..(1_i64 << 52)).contains(id))?;
    let candidate = message["text"].as_str()?.trim();
    (message["chat"]["type"] == "private"
        && message["chat"]["id"].as_i64() == Some(owner)
        && message["from"]["is_bot"] == false
        && message["forward_origin"].is_null()
        && message["forward_date"].is_null()
        && bool::from(candidate.as_bytes().ct_eq(command.as_bytes())))
    .then_some(owner)
}

fn read_env_file(path: &Path) -> Result<String, &'static str> {
    if path
        .ancestors()
        .try_for_each(|part| {
            let metadata = fs::symlink_metadata(part)?;
            if metadata.is_symlink() {
                return Err(io::Error::other(ENV_FILE));
            }
            Ok(())
        })
        .is_err()
        || !path.is_file()
    {
        return Err(ENV_FILE);
    }
    fs::read_to_string(path).map_err(|_| ENV_FILE)
}

fn publish_owner(
    path: &Path,
    original: &str,
    file: &SetupFile<'_>,
    owner: i64,
) -> Result<(), &'static str> {
    let parent = fs::File::open(path.parent().ok_or(ENV_FILE)?).map_err(|_| ENV_FILE)?;
    flock(&parent, FlockOperation::NonBlockingLockExclusive).map_err(|_| CHANGED)?;
    if read_env_file(path)? != original {
        return Err(CHANGED);
    }
    publish_settings(
        &parent,
        path,
        &file.replace_binding(OWNER_SETTING, &owner.to_string()),
    )
}
