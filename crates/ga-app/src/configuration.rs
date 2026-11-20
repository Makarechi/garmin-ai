use std::{
    env, io,
    path::{Path, PathBuf},
};

pub(crate) fn garmin_history_days(value: &str) -> Option<u16> {
    value
        .trim()
        .parse::<u16>()
        .ok()
        .filter(|days| *days <= 3660)
}

pub(crate) fn read<const N: usize>(keys: [&str; N]) -> Result<[Option<String>; N], &'static str> {
    const ERROR: &str = "Storage configuration is invalid";
    let mut values = match dotenvy::from_path_iter(".env") {
        Ok(entries) => file_values(entries, keys),
        Err(dotenvy::Error::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
            std::array::from_fn(|_| None)
        }
        Err(_) => return Err(ERROR),
    };
    for (name, value) in env::vars_os() {
        if let Some(index) = name
            .to_str()
            .and_then(|name| keys.iter().position(|key| name.eq_ignore_ascii_case(key)))
        {
            values[index] = Some(value.into_string().map_err(|_| ERROR)?);
        }
    }
    Ok(values)
}

pub(crate) fn file_values<const N: usize>(
    entries: impl Iterator<Item = Result<(String, String), dotenvy::Error>>,
    keys: [&str; N],
) -> [Option<String>; N] {
    let mut values = std::array::from_fn(|_| None);
    for (name, value) in entries.filter_map(Result::ok) {
        if let Some(index) = keys.iter().position(|key| name.eq_ignore_ascii_case(key)) {
            values[index] = Some(value);
        }
    }
    values
}

pub(crate) fn lock_directory(
    data: &Path,
    tokens: &Path,
    locks: Option<PathBuf>,
) -> Result<PathBuf, &'static str> {
    let absolute = [data, tokens]
        .into_iter()
        .find(|directory| directory.is_absolute());
    match (locks, absolute) {
        (Some(locks), Some(_)) if !locks.is_absolute() => {
            Err("GA_LOCK_DIR must be absolute with absolute storage directories")
        }
        (Some(locks), _) => Ok(locks),
        (None, Some(anchor)) => Ok(anchor
            .parent()
            .ok_or("Invalid storage directory")?
            .join(".state")),
        (None, None) => Ok(PathBuf::from(".state")),
    }
}
