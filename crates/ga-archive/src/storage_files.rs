//! The Python-compatible maintenance lock for file operations and recovery.

use std::{
    env,
    error::Error,
    ffi::OsStr,
    fmt,
    io::Write,
    os::fd::OwnedFd,
    path::{Component, Path},
};

use rustix::{
    fs::{self, AtFlags, FileType, FlockOperation, Mode, OFlags},
    io::Errno,
};

use crate::{
    ArchiveError, DIRECTORY_READ, FILE_MODE, ensure_directory,
    staging::Stage,
    storage_path::{normalized, storage_overlaps},
};

const LOCK: &str = "storage.lock";
const MARKERS: [&str; 2] = ["erased", "activating"];
const ERASING: &str = "erasing";

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum StorageAccess {
    Active,
    Recovery,
}

#[derive(Debug)]
pub enum StorageFilesError {
    Occupied,
    Disabled,
    ErasureIncomplete,
    Unsafe,
    Overlap,
    Filesystem(ArchiveError),
}

impl fmt::Display for StorageFilesError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Occupied => "Stop the worker, login or probe before this operation",
            Self::Disabled => "Restore or resume storage after erasure",
            Self::ErasureIncomplete => {
                "Complete the interrupted erasure before re-enabling storage"
            }
            Self::Unsafe => "Storage lock path redirects or is not a regular file",
            Self::Overlap => "Backup, lock, and private source directories must be independent",
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

pub fn validate_backup_layout(
    data: &Path,
    tokens: &Path,
    locks: &Path,
    backups: &Path,
) -> Result<(), StorageFilesError> {
    let [data, tokens, locks, backups] =
        [data, tokens, locks, backups].map(|path| normalized(path).map_err(ArchiveError::from));
    let (data, tokens, locks, backups) = (data?, tokens?, locks?, backups?);
    if storage_overlaps(&data, &tokens, &locks, &backups).map_err(ArchiveError::from)? {
        return Err(StorageFilesError::Overlap);
    }
    Ok(())
}

/// Hold the original lock file open for the entire local storage operation.
pub struct StorageFilesGuard {
    directory: OwnedFd,
    access: StorageAccess,
    _lock: OwnedFd,
}

impl StorageFilesGuard {
    pub fn acquire(directory: &Path, access: StorageAccess) -> Result<Self, StorageFilesError> {
        let directory = open_directory(directory)?;
        let lock = acquire_lock(&directory, LOCK)?;
        if access == StorageAccess::Active {
            for marker in MARKERS.into_iter().chain([ERASING]) {
                match fs::statat(&directory, marker, AtFlags::SYMLINK_NOFOLLOW) {
                    Ok(_) => return Err(StorageFilesError::Disabled),
                    Err(Errno::NOENT) => {}
                    Err(error) => return Err(error.into()),
                }
            }
        }
        fs::fsync(&directory)?;
        Ok(Self {
            directory,
            access,
            _lock: lock,
        })
    }
    pub fn begin_activation(&self) -> Result<StorageActivation<'_>, StorageFilesError> {
        if self.access != StorageAccess::Recovery {
            return Err(StorageFilesError::Unsafe);
        }
        match fs::statat(&self.directory, ERASING, AtFlags::SYMLINK_NOFOLLOW) {
            Ok(_) => return Err(StorageFilesError::ErasureIncomplete),
            Err(Errno::NOENT) => {}
            Err(error) => return Err(error.into()),
        }
        self.write_marker(
            "activating",
            b"Activation in progress; resume explicitly after interruption.\n",
        )?;
        Ok(StorageActivation { guard: self })
    }

    pub fn begin_erasure(&self) -> Result<StorageErasure<'_>, StorageFilesError> {
        if self.access != StorageAccess::Recovery {
            return Err(StorageFilesError::Unsafe);
        }
        self.write_marker("erased", b"Storage explicitly erased.\n")?;
        self.write_marker(
            ERASING,
            b"Erasure in progress; repeat erasure after interruption.\n",
        )?;
        Ok(StorageErasure { guard: self })
    }

    fn write_marker(&self, name: &str, content: &[u8]) -> Result<(), StorageFilesError> {
        let mut staging = Stage::create(&self.directory)?;
        staging
            .file
            .write_all(content)
            .map_err(ArchiveError::from)?;
        staging.file.sync_all().map_err(ArchiveError::from)?;
        fs::renameat(
            &self.directory,
            staging.name.as_str(),
            &self.directory,
            name,
        )?;
        fs::fsync(&self.directory)?;
        Ok(())
    }
}

/// The legacy erased marker continues to fence the store after erasure completes.
pub struct StorageErasure<'a> {
    guard: &'a StorageFilesGuard,
}

impl StorageErasure<'_> {
    pub fn complete(self) -> Result<(), StorageFilesError> {
        fs::unlinkat(&self.guard.directory, ERASING, AtFlags::empty())?;
        fs::fsync(&self.guard.directory)?;
        Ok(())
    }
}

/// Leave the filesystem fence in place unless the caller confirms the database commit.
pub struct StorageActivation<'a> {
    guard: &'a StorageFilesGuard,
}

impl StorageActivation<'_> {
    pub fn complete(self) -> Result<(), StorageFilesError> {
        for marker in MARKERS {
            match fs::unlinkat(&self.guard.directory, marker, AtFlags::empty()) {
                Ok(()) | Err(Errno::NOENT) => {}
                Err(error) => return Err(error.into()),
            }
            fs::fsync(&self.guard.directory)?;
        }
        Ok(())
    }
}

pub(crate) fn acquire_lock(directory: &OwnedFd, name: &str) -> Result<OwnedFd, StorageFilesError> {
    match fs::statat(directory, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) if FileType::from_raw_mode(stat.st_mode).is_file() => {}
        Ok(_) => return Err(StorageFilesError::Unsafe),
        Err(Errno::NOENT) => {}
        Err(error) => return Err(error.into()),
    }
    let lock = fs::openat(
        directory,
        name,
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
    let current = fs::statat(directory, name, AtFlags::SYMLINK_NOFOLLOW)?;
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) {
        return Err(StorageFilesError::Unsafe);
    }
    Ok(lock)
}

pub(crate) fn open_directory(path: &Path) -> Result<OwnedFd, StorageFilesError> {
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
