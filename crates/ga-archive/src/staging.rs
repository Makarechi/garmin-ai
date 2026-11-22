use std::{fs::File, io, os::fd::OwnedFd};

use rustix::{
    fs::{self, AtFlags, FileType, FlockOperation, Mode, OFlags},
    io::Errno,
    path::Arg,
};

use crate::{
    ArchiveError, DIRECTORY_READ, FILE_MODE,
    filesystem_entry::{Entry, Identity},
};

const STAGE_PREFIX: &str = ".rust-stage-";

pub(crate) fn lock_for_write(root: &OwnedFd) -> Result<OwnedFd, ArchiveError> {
    lock(root, FlockOperation::NonBlockingLockShared)
}

pub(crate) fn remove_orphans(root: &OwnedFd) -> Result<usize, ArchiveError> {
    let exclusive = lock(root, FlockOperation::NonBlockingLockExclusive)?;
    let mut removed = 0;
    for entry in fs::Dir::read_from(&exclusive)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(suffix) = name.to_bytes().strip_prefix(STAGE_PREFIX.as_bytes()) else {
            continue;
        };
        if suffix.len() != 32
            || !suffix
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        {
            continue;
        }
        let stat = fs::statat(&exclusive, name, AtFlags::SYMLINK_NOFOLLOW)?;
        if FileType::from_raw_mode(stat.st_mode).is_file()
            && stat.st_mode & 0o7777 == FILE_MODE.bits()
        {
            fs::unlinkat(&exclusive, name, AtFlags::empty())?;
            removed += 1;
        }
    }
    if removed != 0 {
        fs::fsync(&exclusive)?;
    }
    Ok(removed)
}

pub(crate) fn lock(root: &OwnedFd, operation: FlockOperation) -> Result<OwnedFd, ArchiveError> {
    // Each open creates its own file description, including for threads sharing an Archive.
    let directory = fs::openat(root, ".", DIRECTORY_READ, Mode::empty())?;
    match fs::flock(&directory, operation) {
        Ok(()) => Ok(directory),
        Err(error) if error == Errno::WOULDBLOCK => Err(ArchiveError::StagingBusy),
        Err(error) => Err(error.into()),
    }
}

pub(crate) struct Stage<'a> {
    directory: &'a OwnedFd,
    pub(crate) name: String,
    pub(crate) file: File,
}

impl<'a> Stage<'a> {
    pub(crate) fn create(directory: &'a OwnedFd) -> Result<Self, ArchiveError> {
        Self::create_with_access(directory, OFlags::WRONLY)
    }

    pub(crate) fn create_readable(directory: &'a OwnedFd) -> Result<Self, ArchiveError> {
        Self::create_with_access(directory, OFlags::RDWR)
    }

    fn create_with_access(directory: &'a OwnedFd, access: OFlags) -> Result<Self, ArchiveError> {
        let mut nonce = [0; 16];
        getrandom::fill(&mut nonce).map_err(|_| ArchiveError::StagingEntropy)?;
        let name = format!("{STAGE_PREFIX}{}", hex::encode(nonce));
        let file = fs::openat(
            directory,
            name.as_str(),
            access | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            FILE_MODE,
        )?;
        let stage = Self {
            directory,
            name,
            file: File::from(file),
        };
        fs::fchmod(&stage.file, FILE_MODE)?;
        Ok(stage)
    }
}

impl Drop for Stage<'_> {
    fn drop(&mut self) {
        let _ = fs::unlinkat(self.directory, self.name.as_str(), AtFlags::empty());
    }
}

pub(crate) fn discard_directory(
    parent: &OwnedFd,
    name: impl Arg + Copy,
    directory: &OwnedFd,
    protected: &[Option<Identity>],
) -> io::Result<()> {
    let opened = Entry::opened(directory)?;
    if opened != Entry::lookup(parent, name)?
        || opened.mount != Entry::opened(parent)?.mount
        || protected.contains(&Some(opened.identity))
    {
        return Err(io::Error::other(
            "Backup staging directory changed or overlaps protected storage",
        ));
    }
    walk_contents(directory, opened.mount, protected, false)?;
    walk_contents(directory, opened.mount, protected, true)?;
    if opened != Entry::lookup(parent, name)? {
        return Err(io::Error::other("Backup staging directory changed"));
    }
    fs::unlinkat(parent, name, AtFlags::REMOVEDIR)?;
    fs::fsync(parent)?;
    Ok(())
}

fn walk_contents(
    directory: &OwnedFd,
    mount: u64,
    protected: &[Option<Identity>],
    remove: bool,
) -> io::Result<()> {
    for entry in fs::Dir::read_from(directory)? {
        let entry = entry?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        let entry = Entry::lookup(directory, name)?;
        if entry.mount != mount || protected.contains(&Some(entry.identity)) {
            return Err(io::Error::other(
                "Backup staging contains a mount or protected entry",
            ));
        }
        let flag = if entry.kind == FileType::Directory {
            let child = fs::openat(directory, name, DIRECTORY_READ, Mode::empty())?;
            if Entry::opened(&child)? != entry {
                return Err(io::Error::other("Backup staging entry changed"));
            }
            walk_contents(&child, mount, protected, remove)?;
            AtFlags::REMOVEDIR
        } else {
            AtFlags::empty()
        };
        if remove {
            if Entry::lookup(directory, name)? != entry {
                return Err(io::Error::other("Backup staging entry changed"));
            }
            fs::unlinkat(directory, name, flag)?;
        }
    }
    Ok(())
}
