use std::{
    io::{self, Read, Write},
    os::fd::OwnedFd,
    path::Path,
};

use rustix::fs::{self, FlockOperation};

use crate::{ArchiveError, FILE_MODE, open_private_root, open_regular, staging};

const NAME: &str = "garmin_tokens.json";
const MAX_BYTES: u64 = 64 * 1024;

pub struct GarminTokenFile(OwnedFd);

/// Excludes another native token writer while a session is verified or rotated.
pub struct GarminTokenGuard(OwnedFd);

impl GarminTokenFile {
    pub fn open(directory: &Path) -> Result<Self, ArchiveError> {
        Ok(Self(open_private_root(directory)?))
    }

    pub fn try_lock(&self) -> Result<GarminTokenGuard, ArchiveError> {
        Ok(GarminTokenGuard(staging::lock(
            &self.0,
            FlockOperation::NonBlockingLockExclusive,
        )?))
    }
}

impl GarminTokenGuard {
    /// Read the complete pinned SDK token file through the verified directory.
    pub fn read(&self) -> Result<Option<Vec<u8>>, ArchiveError> {
        let Some(file) = self.open_existing()? else {
            return Ok(None);
        };
        let mut bytes = Vec::new();
        file.take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_BYTES {
            return Err(ArchiveError::PayloadTooLarge);
        }
        Ok(Some(bytes))
    }

    pub fn replace(&self, bytes: &[u8]) -> Result<(), ArchiveError> {
        if bytes.len() as u64 > MAX_BYTES {
            return Err(ArchiveError::PayloadTooLarge);
        }
        self.open_existing()?;
        let mut stage = staging::Stage::create(&self.0)?;
        stage.file.write_all(bytes)?;
        stage.file.sync_all()?;
        fs::renameat(&self.0, stage.name.as_str(), &self.0, NAME)?;
        fs::fsync(&self.0)?;
        Ok(())
    }

    fn open_existing(&self) -> Result<Option<std::fs::File>, ArchiveError> {
        let file = match open_regular(&self.0, NAME) {
            Err(ArchiveError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                return Ok(None);
            }
            result => result?,
        };
        let metadata = fs::fstat(&file)?;
        if metadata.st_nlink != 1 || metadata.st_mode & 0o7777 != FILE_MODE.bits() {
            return Err(ArchiveError::InvalidObject);
        }
        Ok(Some(file))
    }
}
