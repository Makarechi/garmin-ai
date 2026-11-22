//! Validate the private file roots before the database is erased.

use std::{
    env,
    error::Error,
    fmt,
    os::fd::OwnedFd,
    path::{Path, PathBuf},
};

use rustix::{
    fs::{self, AtFlags, Dir, FileType, Mode},
    io::Errno,
    path::Arg,
};

use crate::{
    ArchiveError, DIRECTORY_READ,
    backup_space::existing_directory,
    backup_unpack::resolve_protected_root,
    storage_path::{is_within, normalized, storage_overlaps},
};

#[derive(Debug)]
pub enum ErasureFilesError {
    UnsafePath,
    Overlap,
    Filesystem(ArchiveError),
}

impl fmt::Display for ErasureFilesError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::UnsafePath => "Unsafe erasure directory",
            Self::Overlap => {
                "Erasure directories overlap each other, backups, or the state directory"
            }
            Self::Filesystem(_) => "Private files could not be erased",
        })
    }
}

impl Error for ErasureFilesError {}

impl From<ArchiveError> for ErasureFilesError {
    fn from(error: ArchiveError) -> Self {
        match error {
            ArchiveError::InvalidRoot => Self::UnsafePath,
            error => Self::Filesystem(error),
        }
    }
}

impl From<Errno> for ErasureFilesError {
    fn from(error: Errno) -> Self {
        match error {
            Errno::LOOP | Errno::NOTDIR => Self::UnsafePath,
            error => ArchiveError::from(error).into(),
        }
    }
}

/// Both roots have been checked without removing anything. The caller holds the
/// storage locks and commits the database erasure before removing these files.
pub struct ErasureFiles {
    roots: [ErasureRoot; 2],
}

impl ErasureFiles {
    pub fn prepare(
        data: &Path,
        tokens: &Path,
        locks: &Path,
        backups: &Path,
    ) -> Result<Self, ErasureFilesError> {
        let cwd = env::current_dir()
            .and_then(|path| path.canonicalize())
            .map_err(ArchiveError::from)?;
        let home = protected(&env::home_dir().ok_or(ErasureFilesError::UnsafePath)?)?;
        let roots = [
            ErasureRoot::open(data, &cwd, &home)?,
            ErasureRoot::open(tokens, &cwd, &home)?,
        ];
        let [locks, backups] = [protected(locks)?, protected(backups)?];
        if storage_overlaps(&roots[0].path, &roots[1].path, &locks, &backups)
            .map_err(ArchiveError::from)?
        {
            return Err(ErasureFilesError::Overlap);
        }
        for root in &roots {
            if let Some(directory) = &root.directory {
                walk(directory, false)?;
            }
        }
        Ok(Self { roots })
    }

    pub fn remove(self) -> Result<(), ErasureFilesError> {
        for root in &self.roots {
            root.check_current()?;
        }
        for root in &self.roots {
            root.remove()?;
        }
        Ok(())
    }
}

struct ErasureRoot {
    source: PathBuf,
    path: PathBuf,
    directory: Option<OwnedFd>,
}

impl ErasureRoot {
    fn open(path: &Path, cwd: &Path, home: &Path) -> Result<Self, ErasureFilesError> {
        if path.as_os_str().is_empty() {
            return Err(ErasureFilesError::UnsafePath);
        }
        let source = std::path::absolute(path).map_err(ArchiveError::from)?;
        let (directory, exists) = existing_directory(&source)?;
        let path = if exists {
            path.canonicalize().map_err(ArchiveError::from)?
        } else {
            normalized(path).map_err(ArchiveError::from)?
        };
        if path.components().count() < 4
            || is_within(cwd, &path).map_err(ArchiveError::from)?
            || is_within(home, &path).map_err(ArchiveError::from)?
        {
            return Err(ErasureFilesError::UnsafePath);
        }
        Ok(Self {
            source,
            path,
            directory: exists.then_some(directory),
        })
    }

    fn check_current(&self) -> Result<(), ErasureFilesError> {
        let (directory, exists) = existing_directory(&self.source)?;
        match (exists, &self.directory) {
            (false, None) => Ok(()),
            (true, Some(original)) => {
                let original = fs::fstat(original)?;
                let found = fs::fstat(directory)?;
                if (original.st_dev, original.st_ino) == (found.st_dev, found.st_ino) {
                    Ok(())
                } else {
                    Err(ErasureFilesError::UnsafePath)
                }
            }
            _ => Err(ErasureFilesError::UnsafePath),
        }
    }

    fn remove(&self) -> Result<(), ErasureFilesError> {
        let Some(directory) = &self.directory else {
            return Ok(());
        };
        let parent_path = self.path.parent().ok_or(ErasureFilesError::UnsafePath)?;
        let name = self.path.file_name().ok_or(ErasureFilesError::UnsafePath)?;
        let (parent, exists) = existing_directory(parent_path)?;
        if !exists {
            return Err(ErasureFilesError::UnsafePath);
        }
        let before = fs::fstat(directory)?;
        verify_entry(&parent, name, before)?;
        walk(directory, true)?;
        verify_entry(&parent, name, before)?;
        fs::unlinkat(&parent, name, AtFlags::REMOVEDIR)?;
        fs::fsync(parent)?;
        Ok(())
    }
}

fn protected(path: &Path) -> Result<PathBuf, ErasureFilesError> {
    resolve_protected_root(path).map_err(|_| ErasureFilesError::UnsafePath)
}

fn verify_entry(
    parent: &OwnedFd,
    name: impl Arg,
    opened: fs::Stat,
) -> Result<(), ErasureFilesError> {
    let found = fs::statat(parent, name, AtFlags::SYMLINK_NOFOLLOW)?;
    if (opened.st_dev, opened.st_ino) != (found.st_dev, found.st_ino) {
        return Err(ErasureFilesError::UnsafePath);
    }
    Ok(())
}

fn walk(root: &OwnedFd, remove: bool) -> Result<(), ErasureFilesError> {
    for entry in Dir::read_from(root)? {
        let entry = entry?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        let stat = fs::statat(root, name, AtFlags::SYMLINK_NOFOLLOW)?;
        let flags = match FileType::from_raw_mode(stat.st_mode) {
            FileType::Directory => {
                let child = fs::openat(root, name, DIRECTORY_READ, Mode::empty())?;
                let opened = fs::fstat(&child)?;
                if (stat.st_dev, stat.st_ino) != (opened.st_dev, opened.st_ino) {
                    return Err(ErasureFilesError::UnsafePath);
                }
                walk(&child, remove)?;
                AtFlags::REMOVEDIR
            }
            FileType::Symlink => return Err(ErasureFilesError::UnsafePath),
            _ => AtFlags::empty(),
        };
        if remove {
            verify_entry(root, name, stat)?;
            fs::unlinkat(root, name, flags)?;
        }
    }
    if remove {
        fs::fsync(root)?;
    }
    Ok(())
}
