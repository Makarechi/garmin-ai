use std::{fs::File, io, os::fd::OwnedFd, path::Path};

use rustix::fs;

use crate::{ArchiveError, open_private_root, open_regular, open_root, staging::Stage};

const NAME: &str = "coverage-report.json";

pub struct CoverageReportFile(OwnedFd);

impl CoverageReportFile {
    pub fn open(directory: &Path) -> Result<Self, ArchiveError> {
        Ok(Self(open_private_root(directory)?))
    }

    pub fn read_existing(directory: &Path) -> Result<File, ArchiveError> {
        let root = open_root(directory, false)?;
        open_regular(&root, NAME)
    }

    pub fn checkpoint(
        &self,
        write: impl FnOnce(&mut File) -> io::Result<()>,
    ) -> Result<(), ArchiveError> {
        let mut stage = Stage::create(&self.0)?;
        write(&mut stage.file)?;
        stage.file.sync_all()?;
        fs::renameat(&self.0, stage.name.as_str(), &self.0, NAME)?;
        fs::fsync(&self.0)?;
        Ok(())
    }
}
