use std::{fs::File, num::NonZeroU16, os::fd::OwnedFd, path::Path, time::SystemTime};

use ga_domain::timestamp::StoredInstant;
use rustix::{
    fs::{self, AtFlags, FileType},
    io::Errno,
};

use crate::{
    backup_bundle::CancellableWriter,
    backup_envelope::{
        BackupError, Revision, authenticate_source, open_source, parent_file, revision,
    },
};

pub struct ConfirmedDailyBackup {
    directory: OwnedFd,
    name: String,
    file: File,
    revision: Revision,
}

/// Authenticate and flush the published bundle before reusing it after a restart.
pub fn confirm_daily_backup(
    path: &Path,
    key: &[u8; 32],
    cancelled: impl Fn() -> bool,
) -> Result<ConfirmedDailyBackup, BackupError> {
    let (directory, name) = parent_file(path, false)?;
    let name = name
        .to_str()
        .filter(|name| is_daily_name(name))
        .ok_or(BackupError::InvalidPath)?;
    let file = open_source(path)?;
    let before = revision(&file)?;
    authenticate_source(
        path,
        key,
        &mut CancellableWriter::new(std::io::sink(), cancelled),
    )?;
    if revision(&file)? != before || revision(&open_source(path)?)? != before {
        return Err(BackupError::InvalidPath);
    }
    let verified = ConfirmedDailyBackup {
        directory,
        name: name.to_owned(),
        file,
        revision: before,
    };
    verified.require_current()?;
    verified.file.sync_all()?;
    fs::fsync(&verified.directory)?;
    Ok(verified)
}

impl ConfirmedDailyBackup {
    /// Keep the verified bundle and the newest other daily bundles in the same directory.
    pub fn prune(
        self,
        keep: NonZeroU16,
        cancelled: impl Fn() -> bool,
    ) -> Result<SystemTime, BackupError> {
        self.require_current()?;
        let mut daily = Vec::new();
        for entry in fs::Dir::read_from(&self.directory)? {
            let entry = entry?;
            let Ok(name) = entry.file_name().to_str() else {
                continue;
            };
            if !is_daily_name(name) {
                continue;
            }
            let stat = match fs::statat(&self.directory, name, AtFlags::SYMLINK_NOFOLLOW) {
                Ok(stat) => stat,
                Err(Errno::NOENT) => continue,
                Err(error) => return Err(error.into()),
            };
            if FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile {
                daily.push((name.to_owned(), stat.st_dev, stat.st_ino));
            }
        }
        daily.sort_unstable_by(|(left, _, _), (right, _, _)| {
            (right == &self.name, right).cmp(&(left == &self.name, left))
        });
        for (name, device, inode) in daily.into_iter().skip(usize::from(keep.get())) {
            if cancelled() {
                return Err(
                    std::io::Error::other("Scheduled backup retention was cancelled").into(),
                );
            }
            let current =
                match fs::statat(&self.directory, name.as_str(), AtFlags::SYMLINK_NOFOLLOW) {
                    Ok(stat) => stat,
                    Err(Errno::NOENT) => continue,
                    Err(error) => return Err(error.into()),
                };
            if current.st_dev == device
                && current.st_ino == inode
                && FileType::from_raw_mode(current.st_mode) == FileType::RegularFile
            {
                self.require_current()?;
                fs::unlinkat(&self.directory, name.as_str(), AtFlags::empty())?;
            }
        }
        fs::fsync(&self.directory)?;
        self.require_current()?;
        Ok(self.file.metadata()?.modified()?)
    }

    fn require_current(&self) -> Result<(), BackupError> {
        let previous = fs::fstat(&self.file)?;
        let current = fs::statat(
            &self.directory,
            self.name.as_str(),
            AtFlags::SYMLINK_NOFOLLOW,
        )?;
        if previous.st_dev != current.st_dev
            || previous.st_ino != current.st_ino
            || FileType::from_raw_mode(current.st_mode) != FileType::RegularFile
            || revision(&self.file)? != self.revision
        {
            return Err(BackupError::InvalidPath);
        }
        Ok(())
    }
}

fn is_daily_name(name: &str) -> bool {
    name.strip_prefix("garmin-ai-")
        .and_then(|name| name.strip_suffix(".enc"))
        .is_some_and(|day| {
            day.len() == 10 && format!("{day}T00:00:00Z").parse::<StoredInstant>().is_ok()
        })
}
