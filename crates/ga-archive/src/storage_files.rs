//! The Python-compatible maintenance lock for file operations and recovery.

use std::{
    env,
    error::Error,
    ffi::OsStr,
    fmt,
    os::fd::OwnedFd,
    path::{Component, Path},
};

use rustix::{
    fs::{self, AtFlags, FileType, FlockOperation, Mode, OFlags},
    io::Errno,
};

use crate::{ArchiveError, DIRECTORY_READ, FILE_MODE, ensure_directory};

const LOCK: &str = "storage.lock";
const MARKERS: [&str; 2] = ["erased", "activating"];

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum StorageAccess {
    Active,
    Recovery,
}

#[derive(Debug)]
pub enum StorageFilesError {
    Occupied,
    Disabled,
    Unsafe,
    Filesystem(ArchiveError),
}

impl fmt::Display for StorageFilesError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Occupied => "Stop the worker, login or probe before this operation",
            Self::Disabled => "Restore or resume storage after erasure",
            Self::Unsafe => "Storage lock path redirects or is not a regular file",
            Self::Filesystem(_) => "Storage coordination is unavailable",
        })
    }
}

impl Error for StorageFilesError {}

impl From<Errno> for StorageFilesError {
    fn from(error: Errno) -> Self {
        match error {
            Errno::LOOP | Errno::NOTDIR => Self::Unsafe,
            _ => Self::Filesystem(error.into()),
        }
    }
}

impl From<ArchiveError> for StorageFilesError {
    fn from(error: ArchiveError) -> Self {
        Self::Filesystem(error)
    }
}

/// Hold the original lock file open for the entire local storage operation.
pub struct StorageFilesGuard {
    _lock: OwnedFd,
}

impl StorageFilesGuard {
    pub fn acquire(directory: &Path, access: StorageAccess) -> Result<Self, StorageFilesError> {
        let directory = open_directory(directory)?;
        match fs::statat(&directory, LOCK, AtFlags::SYMLINK_NOFOLLOW) {
            Ok(stat) if FileType::from_raw_mode(stat.st_mode).is_file() => {}
            Ok(_) => return Err(StorageFilesError::Unsafe),
            Err(Errno::NOENT) => {}
            Err(error) => return Err(error.into()),
        }
        let lock = fs::openat(
            &directory,
            LOCK,
            OFlags::CREATE
                | OFlags::RDWR
                | OFlags::NOFOLLOW
                | OFlags::CLOEXEC
                | OFlags::NONBLOCK
                | OFlags::NOCTTY,
            FILE_MODE,
        )?;
        let opened = fs::fstat(&lock)?;
        if !FileType::from_raw_mode(opened.st_mode).is_file() {
            return Err(StorageFilesError::Unsafe);
        }
        match fs::flock(&lock, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => {}
            Err(Errno::WOULDBLOCK) => return Err(StorageFilesError::Occupied),
            Err(error) => return Err(error.into()),
        }
        let current = fs::statat(&directory, LOCK, AtFlags::SYMLINK_NOFOLLOW)?;
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) {
            return Err(StorageFilesError::Unsafe);
        }
        if access == StorageAccess::Active {
            for marker in MARKERS {
                match fs::statat(&directory, marker, AtFlags::SYMLINK_NOFOLLOW) {
                    Ok(_) => return Err(StorageFilesError::Disabled),
                    Err(Errno::NOENT) => {}
                    Err(error) => return Err(error.into()),
                }
            }
        }
        fs::fsync(&directory)?;
        Ok(Self { _lock: lock })
    }
}

fn open_directory(path: &Path) -> Result<OwnedFd, StorageFilesError> {
    if path.as_os_str().is_empty() {
        return Err(StorageFilesError::Unsafe);
    }
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir().map_err(ArchiveError::from)?.join(path)
    };
    let mut directory = fs::open("/", DIRECTORY_READ, Mode::empty())?;
    for component in path.components() {
        let name = match component {
            Component::Normal(name) => name,
            Component::ParentDir => OsStr::new(".."),
            _ => continue,
        };
        directory = match fs::openat(&directory, name, DIRECTORY_READ, Mode::empty()) {
            Ok(child) => child,
            Err(Errno::NOENT) => ensure_directory(&directory, name)?,
            Err(error) => return Err(error.into()),
        };
    }
    Ok(directory)
}
