//! Authenticate and extract to an isolated recovery tree before exclusive publication.

mod format;

use std::{
    fs::File,
    io::{self, BufRead, BufReader, Read, Seek},
    os::{fd::OwnedFd, unix::fs::MetadataExt},
    path::{Component, Path, PathBuf},
};

use flate2::bufread::MultiGzDecoder;
use rustix::{
    fs::{self, AtFlags, Mode, OFlags, RenameFlags},
    io::Errno,
};

use self::format::{Member, invalid};
use crate::{
    DIRECTORY_MODE, DIRECTORY_READ, FILE_MODE,
    backup_bundle::{MAX_DIRECTORY_DEPTH, normalized},
    backup_envelope::{
        BackupDigest, BackupError, authenticate_open_source, open_source, parent_file,
    },
    backup_workspace::{PLAINTEXT_DIRECTORY, PlaintextWorkspace},
    ensure_directory,
    staging::{Stage, remove_contents},
};

const RECOVERY_TREE: &str = "contents";
// The writer counts directories below raw/ or tokens/, excluding the filename.
const MAX_MEMBER_COMPONENTS: usize = MAX_DIRECTORY_DEPTH + 2;

pub fn unpack_backup(
    source: &Path,
    destination: &Path,
    key: &[u8; 32],
    data_dir: &Path,
    token_dir: &Path,
) -> Result<BackupDigest, BackupError> {
    let target = normalized(destination)?;
    let scratch = target
        .parent()
        .ok_or(BackupError::InvalidPath)?
        .join(PLAINTEXT_DIRECTORY);
    if target.components().any(|part| {
        part.as_os_str()
            .as_encoded_bytes()
            .eq_ignore_ascii_case(PLAINTEXT_DIRECTORY.as_bytes())
    }) || is_within(&normalized(source)?, &scratch)?
    {
        return Err(BackupError::InvalidPath);
    }
    for protected in [data_dir, token_dir] {
        let protected = resolve_protected_root(protected)?;
        for path in [&target, &scratch] {
            if is_within(path, &protected)? || is_within(&protected, path)? {
                return Err(BackupError::InvalidPath);
            }
        }
    }
    let (parent, name) = parent_file(destination, true)?;
    match fs::statat(&parent, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(_) => return Err(BackupError::Exists),
        Err(Errno::NOENT) => {}
        Err(error) => return Err(error.into()),
    }
    // Reject missing or redirected sources before reclaiming interrupted staging.
    let input = open_source(source)?;
    let workspace = PlaintextWorkspace::for_recovery(&parent).map_err(BackupError::Workspace)?;
    let mut staged = Stage::create_readable(&workspace.active)?;
    let digest = authenticate_open_source(source, input, key, &mut staged.file)?;
    staged.file.rewind()?;
    let mut raw = BufReader::with_capacity(64 * 1024, &mut staged.file);
    let recovery = RecoveryTree::create(&workspace.active)?;
    if raw.fill_buf()?.starts_with(&[0x1f, 0x8b]) {
        extract(&mut MultiGzDecoder::new(raw), &recovery.root)?;
    } else {
        extract(&mut raw, &recovery.root)?;
    }
    fs::fsync(&recovery.root)?;
    match fs::renameat_with(
        &workspace.active,
        RECOVERY_TREE,
        &parent,
        name,
        RenameFlags::NOREPLACE,
    ) {
        Ok(()) => {}
        Err(Errno::EXIST | Errno::NOTEMPTY) => return Err(BackupError::Exists),
        Err(error) => return Err(error.into()),
    }
    drop(recovery);
    drop(staged);
    drop(workspace);
    fs::fsync(parent)?;
    Ok(digest)
}

fn is_within(path: &Path, directory: &Path) -> io::Result<bool> {
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

    // Different spellings can resolve to the same directory on the recovery volume.
    for ancestor in path.ancestors() {
        if identity(ancestor)? == Some(parent) {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn resolve_protected_root(path: &Path) -> Result<PathBuf, BackupError> {
    let mut resolved = PathBuf::new();
    for part in std::path::absolute(path)?.components() {
        if part == Component::ParentDir {
            resolved.pop();
            continue;
        }
        resolved.push(part.as_os_str());
        match std::fs::symlink_metadata(&resolved) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                resolved = std::fs::canonicalize(&resolved)?;
                if !std::fs::metadata(&resolved)?.is_dir() {
                    return Err(BackupError::InvalidPath);
                }
            }
            Ok(metadata) if metadata.is_dir() => {}
            Ok(_) => return Err(BackupError::InvalidPath),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(resolved)
}

fn extract(stream: &mut impl Read, target: &OwnedFd) -> io::Result<()> {
    let mut database = false;
    while let Some(member) = Member::next(stream)? {
        let path = Path::new(&member.path);
        let parts: Vec<_> = path.components().collect();
        if parts.is_empty()
            || parts.len() > MAX_MEMBER_COMPONENTS
            || parts
                .iter()
                .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err(invalid());
        }
        let valid = match parts[0].as_os_str().to_str() {
            Some("database.jsonl.gz") if parts.len() == 1 => {
                database = true;
                true
            }
            Some("coverage-report.json") if parts.len() == 1 => true,
            Some("raw" | "tokens") if parts.len() > 1 => true,
            _ => false,
        };
        if !valid {
            return Err(invalid());
        }
        let (name, parents) = parts.split_last().ok_or_else(invalid)?;
        let mut directory = rustix::io::dup(target)?;
        for part in parents {
            directory = ensure_directory(&directory, part.as_os_str()).map_err(io::Error::other)?;
        }
        let mut file = File::from(fs::openat(
            &directory,
            name.as_os_str(),
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            FILE_MODE,
        )?);
        fs::fchmod(&file, FILE_MODE)?;
        if io::copy(&mut stream.take(member.bytes), &mut file)? != member.bytes {
            return Err(invalid());
        }
        file.sync_all()?;
        fs::fsync(directory)?;
        format::skip_padding(stream, member.bytes)?;
    }
    if !database {
        return Err(invalid());
    }
    Ok(())
}

struct RecoveryTree<'a> {
    parent: &'a OwnedFd,
    root: OwnedFd,
}

impl<'a> RecoveryTree<'a> {
    fn create(parent: &'a OwnedFd) -> io::Result<Self> {
        fs::mkdirat(parent, RECOVERY_TREE, DIRECTORY_MODE)?;
        let root = fs::openat(parent, RECOVERY_TREE, DIRECTORY_READ, Mode::empty())?;
        Ok(Self { parent, root })
    }
}

impl Drop for RecoveryTree<'_> {
    fn drop(&mut self) {
        if let (Ok(entry), Ok(open)) = (
            fs::statat(self.parent, RECOVERY_TREE, AtFlags::SYMLINK_NOFOLLOW),
            fs::fstat(&self.root),
        ) && (entry.st_dev, entry.st_ino) == (open.st_dev, open.st_ino)
        {
            let _ = remove_contents(&self.root);
            let _ = fs::unlinkat(self.parent, RECOVERY_TREE, AtFlags::REMOVEDIR);
        }
    }
}
