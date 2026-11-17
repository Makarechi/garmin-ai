//! Capacity estimates use open directory handles and expose no path or file contents.

use std::{
    env,
    os::fd::{AsFd, OwnedFd},
    path::{Component, Path},
};

use rustix::{
    fs::{self, AtFlags, Dir, FileType, Mode},
    io::Errno,
};

use crate::{ArchiveError, DIRECTORY_READ};

pub const RESERVE_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Clone, Copy)]
pub enum ExportSize {
    DatabaseBytes(u64),
    EstimatedBytes(u64),
    AlreadyStaged(u64),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VolumeRole {
    Shared,
    Staging,
    Destination,
}

impl VolumeRole {
    pub fn label(self) -> &'static str {
        match self {
            Self::Shared => "shared",
            Self::Staging => "staging",
            Self::Destination => "destination",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Volume {
    pub role: VolumeRole,
    pub free_bytes: u64,
    pub required_bytes: u64,
}

pub struct BackupSpace {
    pub volumes: Vec<Volume>,
}

impl BackupSpace {
    pub fn ready(&self) -> bool {
        self.volumes
            .iter()
            .all(|volume| volume.free_bytes >= volume.required_bytes)
    }
}

pub fn estimate_backup_space(
    data_dir: &Path,
    token_dir: &Path,
    destination: &Path,
    export: ExportSize,
) -> Result<BackupSpace, ArchiveError> {
    let parent = destination.parent().ok_or(ArchiveError::InvalidRoot)?;
    let parent = if parent.as_os_str().is_empty() {
        Path::new(".")
    } else {
        parent
    };
    let (local, _) = existing_directory(&data_dir.join("backup-work"))?;
    let (remote, _) = existing_directory(parent)?;
    let shared = fs::fstat(&local)?.st_dev == fs::fstat(&remote)?.st_dev;
    let export_bytes = match export {
        ExportSize::DatabaseBytes(bytes) => checked(bytes.checked_mul(8))?.max(1024 * 1024),
        ExportSize::EstimatedBytes(bytes) | ExportSize::AlreadyStaged(bytes) => bytes,
    };
    let archive_bytes = checked(export_bytes.checked_add(source_estimate(data_dir, token_dir)?))?;
    let overhead = archive_bytes / 100 + 64 * 1024;
    let local_required = if matches!(export, ExportSize::AlreadyStaged(_)) {
        0
    } else {
        export_bytes
    };
    let remote_required = checked(
        archive_bytes
            .checked_add(overhead)
            .and_then(|total| total.checked_add(64)),
    )?;
    let volumes = if shared {
        vec![Volume {
            role: VolumeRole::Shared,
            free_bytes: free(&local)?,
            required_bytes: checked(
                local_required
                    .checked_add(remote_required)
                    .and_then(|sum| sum.checked_add(RESERVE_BYTES)),
            )?,
        }]
    } else {
        vec![
            Volume {
                role: VolumeRole::Staging,
                free_bytes: free(&local)?,
                required_bytes: checked(local_required.checked_add(RESERVE_BYTES))?,
            },
            Volume {
                role: VolumeRole::Destination,
                free_bytes: free(&remote)?,
                required_bytes: checked(remote_required.checked_add(RESERVE_BYTES))?,
            },
        ]
    };
    Ok(BackupSpace { volumes })
}

fn existing_directory(path: &Path) -> Result<(OwnedFd, bool), ArchiveError> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()?.join(path)
    };
    let mut directory = fs::open("/", DIRECTORY_READ, Mode::empty())?;
    for part in path.components() {
        let name = match part {
            Component::Normal(name) => name,
            Component::ParentDir => std::ffi::OsStr::new(".."),
            _ => continue,
        };
        match fs::openat(&directory, name, DIRECTORY_READ, Mode::empty()) {
            Ok(child) => directory = child,
            Err(Errno::NOENT) => return Ok((directory, false)),
            Err(Errno::NOTDIR | Errno::LOOP) => return Err(ArchiveError::InvalidRoot),
            Err(error) => return Err(error.into()),
        }
    }
    Ok((directory, true))
}

fn source_estimate(data_dir: &Path, token_dir: &Path) -> Result<u64, ArchiveError> {
    let mut tally = Tally::default();
    let (directory, exists) = existing_directory(data_dir)?;
    if exists {
        match fs::statat(directory, "coverage-report.json", AtFlags::SYMLINK_NOFOLLOW) {
            Ok(record) => match FileType::from_raw_mode(record.st_mode) {
                FileType::RegularFile => tally
                    .add(u64::try_from(record.st_size).map_err(|_| ArchiveError::InvalidObject)?)?,
                FileType::Symlink => return Err(ArchiveError::InvalidObject),
                _ => {}
            },
            Err(Errno::NOENT) => {}
            Err(error) => return Err(error.into()),
        }
    }
    for path in [&data_dir.join("raw"), token_dir] {
        let (directory, exists) = existing_directory(path)?;
        if exists {
            walk(directory, &mut tally)?
        }
    }
    let overhead = checked(
        tally
            .files
            .checked_add(1)
            .and_then(|files| files.checked_mul(4096)),
    )?;
    checked(
        tally
            .bytes
            .checked_add(overhead)
            .and_then(|total| total.checked_add(10240)),
    )
}

#[derive(Default)]
struct Tally {
    files: u64,
    bytes: u64,
}

impl Tally {
    fn add(&mut self, bytes: u64) -> Result<(), ArchiveError> {
        self.files = checked(self.files.checked_add(1))?;
        self.bytes = checked(self.bytes.checked_add(bytes))?;
        Ok(())
    }
}

fn walk(root: OwnedFd, tally: &mut Tally) -> Result<(), ArchiveError> {
    let mut pending = vec![Dir::read_from(root)?];
    while let Some(directory) = pending.last_mut() {
        let Some(entry) = directory.next() else {
            pending.pop();
            continue;
        };
        let entry = entry?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        let fd = directory.fd()?;
        let record = fs::statat(fd, name, AtFlags::SYMLINK_NOFOLLOW)?;
        match FileType::from_raw_mode(record.st_mode) {
            FileType::Directory => {
                let opened = fs::openat(fd, name, DIRECTORY_READ, Mode::empty())?;
                pending.push(Dir::read_from(opened)?);
            }
            FileType::RegularFile => tally
                .add(u64::try_from(record.st_size).map_err(|_| ArchiveError::InvalidObject)?)?,
            FileType::Symlink => return Err(ArchiveError::InvalidObject),
            _ => {}
        }
    }
    Ok(())
}

fn free(directory: impl AsFd) -> Result<u64, ArchiveError> {
    let stats = fs::fstatvfs(directory)?;
    checked(stats.f_bavail.checked_mul(stats.f_frsize))
}

fn checked(value: Option<u64>) -> Result<u64, ArchiveError> {
    value.ok_or(ArchiveError::PayloadTooLarge)
}
