use std::{
    io,
    os::unix::fs::MetadataExt,
    path::{Component, Path, PathBuf},
};

pub(crate) fn normalized(path: &Path) -> io::Result<PathBuf> {
    let mut resolved = PathBuf::new();
    for component in std::path::absolute(path)?.components() {
        if component == Component::ParentDir {
            resolved.pop();
        } else {
            resolved.push(component.as_os_str());
        }
    }
    Ok(resolved)
}

pub(crate) fn is_within(path: &Path, directory: &Path) -> io::Result<bool> {
    if path.starts_with(directory) {
        return Ok(true);
    }
    let identity = |path: &Path| match path.metadata() {
        Ok(metadata) => Ok(Some((metadata.dev(), metadata.ino()))),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    };
    let Some(parent) = identity(directory)? else {
        return Ok(false);
    };

    // Different spellings may resolve to the same directory on the volume.
    for ancestor in path.ancestors() {
        if identity(ancestor)? == Some(parent) {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn storage_overlaps(
    data: &Path,
    tokens: &Path,
    locks: &Path,
    backups: &Path,
) -> io::Result<bool> {
    Ok(is_within(data, tokens)?
        || is_within(tokens, data)?
        || is_within(locks, data)?
        || is_within(locks, tokens)?
        || is_within(backups, data)?
        || is_within(backups, tokens)?)
}
