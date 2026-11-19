use std::path::{Component, Path, PathBuf};

use super::{DIRECTORY_ERROR, STORAGE, ValueMap};

pub(super) fn external_sources(
    installation: &Path,
    values: &ValueMap,
) -> Result<Vec<PathBuf>, &'static str> {
    if !installation.is_absolute() {
        return Err(DIRECTORY_ERROR);
    }
    let installation = normalize(installation);
    let mut mounts: Vec<PathBuf> = Vec::new();
    for (name, _) in STORAGE {
        let Some(value) = values.get(name).filter(|value| !value.is_empty()) else {
            continue;
        };
        let path = Path::new(value);
        if !path.is_absolute()
            || (matches!(name, "GA_DATA_DIR" | "GA_TOKEN_DIR") && path.components().count() < 4)
            || value.contains([',', '\n', '\r'])
        {
            return Err(
                "Existing storage mounts must be dedicated absolute paths without commas or newlines",
            );
        }
        let path = normalize(path);
        if path.starts_with(&installation) {
            continue;
        }
        // Preserve ownership checks in the child directory when Docker presents
        // a portable bind mount's root with different ownership.
        let parent = if path.components().count() >= 4 {
            path.parent().ok_or(DIRECTORY_ERROR)?
        } else {
            &path
        };
        if parent.parent().is_none() || installation.starts_with(parent) {
            return Err(DIRECTORY_ERROR);
        }
        if mounts.iter().any(|installed| parent.starts_with(installed)) {
            continue;
        }
        mounts.retain(|installed| !installed.starts_with(parent));
        mounts.push(parent.to_owned());
    }
    Ok(mounts)
}

fn normalize(path: &Path) -> PathBuf {
    let mut absolute = PathBuf::new();
    for part in path.components() {
        if part == Component::ParentDir {
            absolute.pop();
        } else {
            absolute.push(part.as_os_str());
        }
    }
    absolute
}
