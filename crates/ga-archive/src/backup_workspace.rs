//! Exclusive private database staging shared with Python backup operations.

use std::{
    io,
    os::fd::OwnedFd,
    path::{Path, PathBuf},
};

use rustix::{
    fs::{self, AtFlags, Mode},
    io::Errno,
};

use crate::{
    ArchiveError, DIRECTORY_MODE, DIRECTORY_READ, ensure_directory,
    staging::remove_contents,
    storage_files::{StorageFilesError, acquire_lock, open_directory},
};

const ACTIVE: &str = "active";

pub struct BackupWorkspace {
    directory: OwnedFd,
    active: OwnedFd,
    export: PathBuf,
    _operation: OwnedFd,
}

impl BackupWorkspace {
    pub fn acquire(data: &Path) -> Result<Self, StorageFilesError> {
        let root = data.join("backup-work").join(".garmin-ai-plaintext");
        let directory = open_directory(&root)?;
        fs::fchmod(&directory, DIRECTORY_MODE)?;
        let operation = acquire_lock(&directory, "operation.lock")?;
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
            export: root.join(ACTIVE).join("database.jsonl.gz"),
            _operation: operation,
        })
    }

    pub fn export_path(&self) -> &Path {
        &self.export
    }
}

impl Drop for BackupWorkspace {
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
