use std::{
    env, io,
    path::{Path, PathBuf},
};

pub(crate) fn read<const N: usize>(keys: [&str; N]) -> Result<[Option<String>; N], &'static str> {
    const ERROR: &str = "Storage configuration is invalid";
    let mut values = std::array::from_fn(|_| None);
    match dotenvy::from_path_iter(".env") {
        Ok(entries) => {
            for (name, value) in entries.filter_map(Result::ok) {
                if let Some(index) = keys.iter().position(|key| name.eq_ignore_ascii_case(key)) {
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
            .and_then(|name| keys.iter().position(|key| name.eq_ignore_ascii_case(key)))
        {
            values[index] = Some(value.into_string().map_err(|_| ERROR)?);
        }
    }
    Ok(values)
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
