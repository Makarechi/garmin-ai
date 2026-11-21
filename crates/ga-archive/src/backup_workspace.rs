//! Exclusive private database staging shared with Python backup operations.

use std::{
    io,
    os::fd::OwnedFd,
    path::{Path, PathBuf},
};

use rustix::{
    fs::{self, AtFlags, Mode},
    io::Errno,
    process::geteuid,
};

use crate::{
    ArchiveError, DIRECTORY_MODE, DIRECTORY_READ, ensure_directory,
    staging::remove_contents,
    storage_files::{StorageFilesError, acquire_lock, open_directory},
};

pub(crate) const PLAINTEXT_DIRECTORY: &str = ".garmin-ai-plaintext";
const ACTIVE: &str = "active";
const OPERATION: &str = "operation.lock";

pub struct BackupWorkspace {
    _workspace: PlaintextWorkspace,
    export: PathBuf,
}

impl BackupWorkspace {
    pub fn acquire(data: &Path) -> Result<Self, StorageFilesError> {
        let root = data.join("backup-work").join(PLAINTEXT_DIRECTORY);
        let directory = open_directory(&root)?;
        fs::fchmod(&directory, DIRECTORY_MODE)?;
        Ok(Self {
            _workspace: PlaintextWorkspace::acquire(directory)?,
            export: root.join(ACTIVE).join("database.jsonl.gz"),
        })
    }

    pub fn export_path(&self) -> &Path {
        &self.export
    }
}

pub(crate) struct PlaintextWorkspace {
    directory: OwnedFd,
    pub(crate) active: OwnedFd,
    _operation: OwnedFd,
}

impl PlaintextWorkspace {
    pub(crate) fn for_recovery(parent: &OwnedFd) -> Result<Self, StorageFilesError> {
        let directory = ensure_directory(parent, PLAINTEXT_DIRECTORY.as_ref())?;
        let stat = fs::fstat(&directory)?;
        if stat.st_uid != geteuid().as_raw()
            || Mode::from_raw_mode(stat.st_mode).intersects(Mode::RWXG | Mode::RWXO)
        {
            return Err(StorageFilesError::Unsafe);
        }
        // A managed operation creates its persistent lock before the active tree.
        if fs::statat(&directory, ACTIVE, AtFlags::SYMLINK_NOFOLLOW).is_ok()
            && matches!(
                fs::statat(&directory, OPERATION, AtFlags::SYMLINK_NOFOLLOW),
                Err(Errno::NOENT)
            )
        {
            return Err(StorageFilesError::Unsafe);
        }
        Self::acquire(directory)
    }

    fn acquire(directory: OwnedFd) -> Result<Self, StorageFilesError> {
        // Python's backup and recovery commands use the same private operation lock.
        let operation = acquire_lock(&directory, OPERATION)?;
        match fs::openat(&directory, ACTIVE, DIRECTORY_READ, Mode::empty()) {
            Ok(previous) => discard(&directory, &previous).map_err(ArchiveError::from)?,
            Err(Errno::NOENT) => {}
            Err(error) => return Err(error.into()),
        }
        fs::fsync(&directory)?;
        let active = ensure_directory(&directory, ACTIVE.as_ref())?;
        fs::fchmod(&active, DIRECTORY_MODE)?;
        fs::fsync(&active)?;
        Ok(Self {
            directory,
            active,
            _operation: operation,
        })
    }
}

impl Drop for PlaintextWorkspace {
    fn drop(&mut self) {
        let _ = discard(&self.directory, &self.active);
    }
}

fn discard(directory: &OwnedFd, active: &OwnedFd) -> io::Result<()> {
    let opened = fs::fstat(active)?;
    let current = fs::statat(directory, ACTIVE, AtFlags::SYMLINK_NOFOLLOW)?;
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) {
        return Err(io::Error::other("Backup staging directory changed"));
    }
    remove_contents(active)?;
    fs::unlinkat(directory, ACTIVE, AtFlags::REMOVEDIR)?;
    fs::fsync(directory)?;
    Ok(())
}
