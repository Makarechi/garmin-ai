//! Exclusive private database staging shared with Python backup operations.

use std::{
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
    filesystem_entry::{Entry, Identity, directory_identity},
    staging::discard_directory,
    storage_files::{StorageFilesError, acquire_lock, open_directory},
};

pub(crate) const PLAINTEXT_DIRECTORY: &str = ".garmin-ai-plaintext";
const ACTIVE: &str = "active";
const BACKUP_WORK: &str = "backup-work";
const OPERATION: &str = "operation.lock";
type Protected = [Option<Identity>; 3];

pub struct BackupWorkspace {
    _workspace: PlaintextWorkspace,
    export: PathBuf,
}

impl BackupWorkspace {
    pub fn acquire(data: &Path, tokens: &Path) -> Result<Self, StorageFilesError> {
        let root = data.join(BACKUP_WORK).join(PLAINTEXT_DIRECTORY);
        let data = open_directory(data)?;
        let protected = [
            Some(Entry::opened(&data).map_err(ArchiveError::from)?.identity),
            directory_identity(tokens).map_err(ArchiveError::from)?,
            None,
        ];
        let parent = ensure_directory(&data, BACKUP_WORK.as_ref())?;
        if protected.contains(&Some(
            Entry::opened(&parent).map_err(ArchiveError::from)?.identity,
        )) {
            return Err(StorageFilesError::Unsafe);
        }
        let directory = private_child(&parent, PLAINTEXT_DIRECTORY, &protected)?;
        fs::fchmod(&directory, DIRECTORY_MODE)?;
        Ok(Self {
            _workspace: PlaintextWorkspace::acquire(directory, protected)?,
            export: root.join(ACTIVE).join("database.jsonl.gz"),
        })
    }

    #[must_use]
    pub fn export_path(&self) -> &Path {
        &self.export
    }
}

pub(crate) struct PlaintextWorkspace {
    directory: OwnedFd,
    pub(crate) active: OwnedFd,
    _operation: OwnedFd,
    protected: Protected,
}

impl PlaintextWorkspace {
    pub(crate) fn for_recovery(
        parent: &OwnedFd,
        protected: Protected,
    ) -> Result<Self, StorageFilesError> {
        let directory = private_child(parent, PLAINTEXT_DIRECTORY, &protected)?;
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
        Self::acquire(directory, protected)
    }

    fn acquire(directory: OwnedFd, protected: Protected) -> Result<Self, StorageFilesError> {
        // Python's backup and recovery commands use the same private operation lock.
        let operation = acquire_lock(&directory, OPERATION)?;
        match fs::openat(&directory, ACTIVE, DIRECTORY_READ, Mode::empty()) {
            Ok(previous) => discard_directory(&directory, ACTIVE, &previous, &protected)
                .map_err(ArchiveError::from)?,
            Err(Errno::NOENT) => {}
            Err(error) => return Err(error.into()),
        }
        fs::fsync(&directory)?;
        let active = private_child(&directory, ACTIVE, &protected)?;
        fs::fchmod(&active, DIRECTORY_MODE)?;
        fs::fsync(&active)?;
        Ok(Self {
            directory,
            active,
            _operation: operation,
            protected,
        })
    }
}

impl Drop for PlaintextWorkspace {
    fn drop(&mut self) {
        let _ = discard_directory(&self.directory, ACTIVE, &self.active, &self.protected);
    }
}

fn private_child(
    parent: &OwnedFd,
    name: &str,
    protected: &Protected,
) -> Result<OwnedFd, StorageFilesError> {
    let directory = ensure_directory(parent, name.as_ref())?;
    let entry = Entry::opened(&directory).map_err(ArchiveError::from)?;
    if entry.mount != Entry::opened(parent).map_err(ArchiveError::from)?.mount
        || protected.contains(&Some(entry.identity))
    {
        return Err(StorageFilesError::Unsafe);
    }
    Ok(directory)
}
