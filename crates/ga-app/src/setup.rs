mod database;
mod dotenv;
mod integrations;
mod mounts;
mod pairing;
mod preferences;

pub(crate) use pairing::pair_telegram;

use std::{
    collections::BTreeMap,
    env, fs,
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::{Component, Path, PathBuf},
};

use base64::{
    Engine,
    engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD},
};
use clap::Args;
use ga_domain::{
    event_input::known_timezone, event_payload::legacy_boolean,
    language_tags::normalize_language_tag,
};
use rustix::{
    fs::{FlockOperation, flock},
    process::{getgid, getuid},
};
use serde_json::Value;

use crate::{
    authorization::{Authorization, is_placeholder},
    backup::decode_key,
    configuration::garmin_history_days,
};
use dotenv::SetupFile;

const INVALID_SETTINGS: &str =
    "Invalid preserved runtime settings; existing settings were not changed";
const FILE_ERROR: &str = "Could not prepare the local settings file";
const DIRECTORY_ERROR: &str = "Use dedicated private storage directories";
const STORAGE: [(&str, &str); 4] = [
    ("GA_DATA_DIR", "data"),
    ("GA_TOKEN_DIR", "tokens/garmin"),
    ("GA_LOCK_DIR", ".state"),
    ("GA_BACKUP_DIR", "backups"),
];
type ValueMap = BTreeMap<String, String>;

#[derive(Args)]
pub(crate) struct Options {
    #[arg(long)]
    instance: Option<String>,
    #[arg(long)]
    db_port: Option<u16>,
    #[arg(long)]
    api_port: Option<u16>,
    #[arg(long, value_parser = ["en", "ru"])]
    locale: Option<String>,
    #[arg(long)]
    timezone: Option<String>,
    #[arg(long, value_parser = ["metric", "imperial"])]
    units: Option<String>,
    #[arg(long, value_name = "INSTALLATION", hide = true, conflicts_with_all = [
        "instance", "db_port", "api_port", "locale", "timezone", "units"
    ])]
    mount_sources: Option<PathBuf>,
}

pub(crate) fn run(options: Options) -> Result<(), &'static str> {
    if let Some(installation) = &options.mount_sources {
        let original = read_settings(Path::new(".env"))?;
        for directory in
            mounts::external_sources(installation, &SetupFile::parse(&original)?.values)?
        {
            println!("{}", directory.display());
        }
        return Ok(());
    }
    let root = env::current_dir().map_err(|_| DIRECTORY_ERROR)?;
    let directory = fs::File::open(&root).map_err(|_| DIRECTORY_ERROR)?;
    flock(&directory, FlockOperation::NonBlockingLockExclusive)
        .map_err(|_| "Another setup is using this installation directory")?;
    let path = root.join(".env");
    let original = read_settings(&path)?;
    let file = SetupFile::parse(&original)?;
    let mut values = file.values.clone();
    let db_port = instance_settings(&mut values, &options)?;
    defaults(&mut values, options)?;
    database::prepare(&mut values, db_port)?;
    validate_runtime(&values)?;
    prepare_directories(&mut values, &root)?;
    publish_settings(&directory, &path, &file.render(&values))?;
    println!(
        "Local settings prepared. Existing configured secrets preserved; placeholders replaced."
    );
    Ok(())
}

fn publish_settings(parent: &fs::File, path: &Path, content: &str) -> Result<(), &'static str> {
    let directory = path.parent().ok_or(FILE_ERROR)?;
    let mut prepared = tempfile::NamedTempFile::new_in(directory).map_err(|_| FILE_ERROR)?;
    prepared
        .write_all(content.as_bytes())
        .and_then(|()| prepared.as_file().sync_all())
        .map_err(|_| FILE_ERROR)?;
    prepared.persist(path).map_err(|_| FILE_ERROR)?;
    parent.sync_all().map_err(|_| FILE_ERROR)
}

fn read_settings(path: &Path) -> Result<String, &'static str> {
    match fs::read_to_string(path) {
        Ok(content) => Ok(content),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(String::new()),
        Err(_) => Err(FILE_ERROR),
    }
}

fn instance_settings(values: &mut ValueMap, options: &Options) -> Result<u16, &'static str> {
    let initialized = database::configured_url(values, "GA_DATABASE_URL").is_some();
    for (key, requested, default) in [
        (
            "COMPOSE_PROJECT_NAME",
            options.instance.clone(),
            "garmin-ai",
        ),
        (
            "GA_DB_PORT",
            options.db_port.map(|port| port.to_string()),
            "55432",
        ),
        (
            "GA_API_PORT",
            options.api_port.map(|port| port.to_string()),
            "8080",
        ),
    ] {
        let existing = values
            .get(key)
            .filter(|value| !value.is_empty())
            .map_or(default, String::as_str);
        if let Some(requested) = &requested
            && initialized
            && requested != existing
        {
            return Err("Existing instance identity and ports cannot be changed by setup");
        }
        let value = requested.unwrap_or_else(|| existing.to_owned());
        values.insert(key.to_owned(), value);
    }
    let instance = &values["COMPOSE_PROJECT_NAME"];
    let alphanumeric = |byte: u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
    if instance.len() > 63
        || !instance.bytes().next().is_some_and(alphanumeric)
        || !instance
            .bytes()
            .all(|byte| alphanumeric(byte) || matches!(byte, b'_' | b'-'))
    {
        return Err("Instance name must use lowercase letters, digits, underscores or hyphens");
    }
    let [Some(db), Some(api)] = ["GA_DB_PORT", "GA_API_PORT"]
        .map(|key| values[key].parse::<u16>().ok().filter(|port| *port >= 1024))
    else {
        return Err("Instance ports must be distinct values from 1024 to 65535");
    };
    if db == api {
        return Err("Instance ports must be distinct values from 1024 to 65535");
    }
    Ok(db)
}

fn defaults(values: &mut ValueMap, options: Options) -> Result<(), &'static str> {
    for (key, requested, default) in [
        ("GA_LOCALE", options.locale, "ru"),
        ("GA_TIMEZONE", options.timezone, "Europe/Bratislava"),
        ("GA_UNITS", options.units, "metric"),
    ] {
        if let Some(requested) = requested {
            values.insert(key.to_owned(), requested);
        } else {
            set_default(values, key, default);
        }
    }
    for (key, value) in STORAGE.into_iter().chain([
        ("GA_LLM_ENABLED", "false"),
        ("GA_LLM_CONSENT", "null"),
        ("GA_PROACTIVE_ENABLED", "false"),
    ]) {
        set_default(values, key, value);
    }
    set_default(values, "GA_APP_UID", &getuid().as_raw().to_string());
    set_default(values, "GA_APP_GID", &getgid().as_raw().to_string());
    if configured(values, "GA_BACKUP_KEY").is_none() {
        values.insert("GA_BACKUP_KEY".into(), URL_SAFE.encode(random::<32>()?));
    }
    if configured(values, "GA_API_KEY").is_none_or(is_placeholder) {
        let scoped = values
            .get("GA_API_TOKENS")
            .filter(|value| !value.is_empty())
            .map_or("[]", String::as_str);
        let tokens: Vec<Value> = serde_json::from_str(scoped).map_err(|_| INVALID_SETTINGS)?;
        let key = if tokens.is_empty() {
            URL_SAFE_NO_PAD.encode(random::<40>()?)
        } else {
            String::new()
        };
        values.insert("GA_API_KEY".into(), key);
    }
    Ok(())
}

fn validate_runtime(values: &ValueMap) -> Result<(), &'static str> {
    if !known_timezone(&values["GA_TIMEZONE"]) {
        return Err(
            "GA_TIMEZONE must name a valid IANA timezone; existing settings were not changed",
        );
    }
    if normalize_language_tag(&values["GA_LOCALE"]).is_none()
        || !matches!(values["GA_UNITS"].as_str(), "metric" | "imperial")
    {
        return Err(INVALID_SETTINGS);
    }
    let legacy = values["GA_API_KEY"].as_str();
    if !legacy.is_empty() && legacy.chars().count() < 32 {
        return Err(
            "GA_API_KEY must contain at least 32 characters; existing settings were not changed",
        );
    }
    let scoped = values
        .get("GA_API_TOKENS")
        .filter(|value| !value.is_empty())
        .map(String::as_str);
    Authorization::parse(Some(legacy), scoped).ok_or(INVALID_SETTINGS)?;
    decode_key(&values["GA_BACKUP_KEY"])
        .map_err(|_| "GA_BACKUP_KEY must encode exactly 32 bytes")?;
    for key in [
        "GA_LLM_ENABLED",
        "GA_PROACTIVE_ENABLED",
        "GA_MCP_ENABLE_WRITES",
        "GA_GEMINI_FALLBACK_ENABLED",
    ] {
        if values
            .get(key)
            .is_some_and(|value| legacy_boolean(&Value::String(value.clone())).is_none())
        {
            return Err(INVALID_SETTINGS);
        }
    }
    for (key, min, max) in [
        ("GA_BACKUP_KEEP_DAILY", 1, 365),
        ("GA_QUESTION_BUDGET", 0, 20),
        ("GA_QUIET_START_HOUR", 0, 23),
        ("GA_QUIET_END_HOUR", 0, 23),
    ] {
        if values.get(key).is_some_and(|value| {
            value
                .parse::<u16>()
                .ok()
                .is_none_or(|value| !(min..=max).contains(&value))
        }) {
            return Err(INVALID_SETTINGS);
        }
    }
    integrations::validate(
        values.get("GA_INTEGRATIONS").map(String::as_str),
        values.get("GA_TELEGRAM_USER_ID").map(String::as_str),
    )?;
    if values
        .get("GA_BACKFILL_DAYS")
        .is_some_and(|raw| garmin_history_days(raw).is_none())
    {
        return Err(INVALID_SETTINGS);
    }
    preferences::validate(values)?;
    ga_app::model_gateway::RuntimeModelSettings::validate_saved_consent(&values["GA_LLM_CONSENT"])
        .map_err(|_| INVALID_SETTINGS)
}

fn prepare_directories(values: &mut ValueMap, root: &Path) -> Result<(), &'static str> {
    let uid = values["GA_APP_UID"]
        .parse::<u32>()
        .ok()
        .filter(|uid| *uid > 0);
    let gid = values["GA_APP_GID"].parse::<u32>().ok();
    let (Some(uid), Some(gid)) = (uid, gid) else {
        return Err("Configure a non-root service UID and a valid GID");
    };
    let home = env::home_dir().and_then(|home| fs::canonicalize(home).ok());
    let paths = STORAGE.map(|(name, _)| {
        let value = &values[name];
        resolve_directory(value, home.as_deref())
    });
    let [data, tokens, locks, backups] = paths;
    let [data, tokens, locks, backups] = [data?, tokens?, locks?, backups?];
    if [&data, &tokens]
        .iter()
        .any(|path| path.components().count() < 4)
    {
        return Err("Use a dedicated source directory at least three levels below root");
    }
    let paths = [data, tokens, locks, backups];
    check_layout(&paths, root, home.as_deref())?;
    for path in &paths {
        if let Ok(info) = path.metadata()
            && (info.uid(), info.gid()) != (uid, gid)
        {
            return Err("Storage ownership must match GA_APP_UID and GA_APP_GID");
        }
    }
    let archive = paths[0].join("raw");
    match fs::symlink_metadata(&archive) {
        Ok(info) if info.is_dir() && (info.uid(), info.gid()) == (uid, gid) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        _ => return Err("Raw archive must be a private directory owned by the service user"),
    }
    for path in &paths {
        create_owned_directory(path, uid, gid)?;
    }
    // A case-insensitive filesystem may resolve different spellings to the
    // same directory only once the paths have been created.
    check_layout(&paths, root, home.as_deref())?;
    for ((name, _), path) in STORAGE.into_iter().zip(paths) {
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
            .map_err(|_| DIRECTORY_ERROR)?;
        values.insert(
            name.into(),
            path.into_os_string()
                .into_string()
                .map_err(|_| DIRECTORY_ERROR)?,
        );
    }
    create_owned_directory(&archive, uid, gid)?;
    fs::set_permissions(archive, fs::Permissions::from_mode(0o700)).map_err(|_| DIRECTORY_ERROR)
}

fn create_owned_directory(path: &Path, uid: u32, gid: u32) -> Result<(), &'static str> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .map_err(|_| DIRECTORY_ERROR)?;
    let metadata = path.metadata().map_err(|_| DIRECTORY_ERROR)?;
    if (metadata.uid(), metadata.gid()) != (uid, gid) {
        return Err("Storage ownership must match GA_APP_UID and GA_APP_GID");
    }
    Ok(())
}

fn check_layout(
    paths: &[PathBuf; 4],
    root: &Path,
    home: Option<&Path>,
) -> Result<(), &'static str> {
    for path in paths {
        if within(root, path) || home.is_some_and(|home| same_directory(home, path)) {
            return Err(DIRECTORY_ERROR);
        }
    }
    let [data, tokens, locks, backups] = paths;
    if within(data, tokens) || within(tokens, data) {
        return Err("Data and token directories must not overlap");
    }
    if within(backups, data) || within(backups, tokens) {
        return Err("Backups must be outside private source directories");
    }
    if within(locks, data) || within(locks, tokens) {
        return Err("Lock directory must be outside private source directories");
    }
    Ok(())
}

fn same_directory(left: &Path, right: &Path) -> bool {
    left == right
        || left
            .metadata()
            .ok()
            .zip(right.metadata().ok())
            .is_some_and(|(left, right)| (left.dev(), left.ino()) == (right.dev(), right.ino()))
}

fn within(child: &Path, parent: &Path) -> bool {
    child.starts_with(parent)
        || child
            .ancestors()
            .any(|directory| same_directory(directory, parent))
}

fn resolve_directory(value: &str, home: Option<&Path>) -> Result<PathBuf, &'static str> {
    let path = match value {
        "~" => home.ok_or(DIRECTORY_ERROR)?.to_path_buf(),
        value if value.starts_with("~/") => home.ok_or(DIRECTORY_ERROR)?.join(&value[2..]),
        value => PathBuf::from(value),
    };
    let absolute = std::path::absolute(path).map_err(|_| DIRECTORY_ERROR)?;
    let mut resolved = PathBuf::new();
    for part in absolute.components() {
        if part == Component::ParentDir {
            resolved.pop();
            continue;
        }
        resolved.push(part.as_os_str());
        match fs::symlink_metadata(&resolved) {
            Ok(metadata) if metadata.is_dir() => {}
            Ok(metadata) if metadata.is_symlink() => {
                resolved = fs::canonicalize(&resolved).map_err(|_| DIRECTORY_ERROR)?;
                if !resolved.is_dir() {
                    return Err(DIRECTORY_ERROR);
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            _ => return Err(DIRECTORY_ERROR),
        }
    }
    Ok(resolved)
}

fn configured<'a>(values: &'a ValueMap, key: &str) -> Option<&'a str> {
    values
        .get(key)
        .map(String::as_str)
        .filter(|value| !value.is_empty())
}

fn set_default(values: &mut ValueMap, key: &str, default: &str) {
    if configured(values, key).is_none() {
        values.insert(key.into(), default.to_owned());
    }
}

fn random<const N: usize>() -> Result<[u8; N], &'static str> {
    let mut bytes = [0; N];
    getrandom::fill(&mut bytes).map_err(|_| "Could not generate private settings")?;
    Ok(bytes)
}
