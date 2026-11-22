//! Validate the private file roots before the database is erased.

use std::{
    env,
    error::Error,
    fmt, io,
    os::fd::OwnedFd,
    path::{Path, PathBuf},
};

use rustix::{
    fs::{self, AtFlags, Dir, FileType, Mode},
    io::Errno,
    path::Arg,
};

use crate::{
    ArchiveError, DIRECTORY_READ,
    backup_space::existing_directory,
    backup_unpack::resolve_protected_root,
    filesystem_entry::{Entry, Identity, directory_identity},
    storage_path::{is_within, normalized, storage_overlaps},
};

#[derive(Debug)]
pub enum ErasureFilesError {
    UnsafePath,
    Overlap,
    NestedMount,
    Filesystem(ArchiveError),
}

impl fmt::Display for ErasureFilesError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::UnsafePath => "Unsafe erasure directory",
            Self::Overlap => {
                "Erasure directories overlap each other, backups, or the state directory"
            }
            Self::NestedMount => "Erasure directories must not contain nested mounts",
            Self::Filesystem(_) => "Private files could not be erased",
        })
    }
}

impl Error for ErasureFilesError {}

impl From<io::Error> for ErasureFilesError {
    fn from(error: io::Error) -> Self {
        if error.raw_os_error().is_some_and(|code| {
            code == Errno::LOOP.raw_os_error() || code == Errno::NOTDIR.raw_os_error()
        }) {
            Self::UnsafePath
        } else {
            ArchiveError::from(error).into()
        }
    }
}

impl From<ArchiveError> for ErasureFilesError {
    fn from(error: ArchiveError) -> Self {
        match error {
            ArchiveError::InvalidRoot => Self::UnsafePath,
            error => Self::Filesystem(error),
        }
    }
}

impl From<Errno> for ErasureFilesError {
    fn from(error: Errno) -> Self {
        match error {
            Errno::LOOP | Errno::NOTDIR => Self::UnsafePath,
            error => ArchiveError::from(error).into(),
        }
    }
}

/// Both roots have been checked without removing anything. The caller holds the
/// storage locks and commits the database erasure before removing these files.
pub struct ErasureFiles {
    roots: [ErasureRoot; 2],
    protected_paths: [PathBuf; 2],
}

impl ErasureFiles {
    pub fn prepare(
        data: &Path,
        tokens: &Path,
        locks: &Path,
        backups: &Path,
    ) -> Result<Self, ErasureFilesError> {
        let cwd = env::current_dir()
            .and_then(|path| path.canonicalize())
            .map_err(ArchiveError::from)?;
        let home = protected(&env::home_dir().ok_or(ErasureFilesError::UnsafePath)?)?;
        let roots = [
            ErasureRoot::open(data, &cwd, &home)?,
            ErasureRoot::open(tokens, &cwd, &home)?,
        ];
        if storage_overlaps(
            &roots[0].path,
            &roots[1].path,
            &protected(locks)?,
            &protected(backups)?,
        )
        .map_err(ArchiveError::from)?
        {
            return Err(ErasureFilesError::Overlap);
        }
        let files = Self {
            roots,
            protected_paths: [locks.to_path_buf(), backups.to_path_buf()],
        };
        let identities = files.protected_identities()?;
        for root in &files.roots {
            if let Some(directory) = &root.directory {
                walk(
                    directory,
                    Entry::opened(directory)?.mount,
                    &identities,
                    false,
                )?;
            }
        }
        Ok(files)
    }

    pub fn remove(self) -> Result<(), ErasureFilesError> {
        for root in &self.roots {
            root.check_current()?;
        }
        let identities = self.protected_identities()?;
        for root in &self.roots {
            root.remove(&identities)?;
        }
        Ok(())
    }

    fn protected_identities(&self) -> Result<[Option<Identity>; 4], ErasureFilesError> {
        let [locks, backups] = self.protected_paths.each_ref();
        let [data, tokens] = [self.roots[0].identity()?, self.roots[1].identity()?];
        let [locks, backups] = [directory_identity(locks)?, directory_identity(backups)?];
        if (data.is_some() && data == tokens)
            || [data, tokens]
                .into_iter()
                .flatten()
                .any(|identity| [locks, backups].contains(&Some(identity)))
        {
            return Err(ErasureFilesError::Overlap);
        }
        Ok([data, tokens, locks, backups])
    }
}

struct ErasureRoot {
    source: PathBuf,
    path: PathBuf,
    directory: Option<OwnedFd>,
}

impl ErasureRoot {
    fn open(path: &Path, cwd: &Path, home: &Path) -> Result<Self, ErasureFilesError> {
        if path.as_os_str().is_empty() {
            return Err(ErasureFilesError::UnsafePath);
        }
        let source = std::path::absolute(path).map_err(ArchiveError::from)?;
        let (directory, exists) = existing_directory(&source)?;
        let path = if exists {
            path.canonicalize().map_err(ArchiveError::from)?
        } else {
            normalized(path).map_err(ArchiveError::from)?
        };
        if is_within(cwd, &path).map_err(ArchiveError::from)?
            || is_within(home, &path).map_err(ArchiveError::from)?
        {
            return Err(ErasureFilesError::UnsafePath);
        }
        let depth = path.components().count();
        if depth < 4
            && !(depth == 3
                && exists
                && Entry::opened(&directory)?.mount != Entry::opened(&open_parent(&path)?)?.mount)
        {
            return Err(ErasureFilesError::UnsafePath);
        }
        Ok(Self {
            source,
            path,
            directory: exists.then_some(directory),
        })
    }

    fn identity(&self) -> Result<Option<Identity>, ErasureFilesError> {
        self.directory
            .as_ref()
            .map(|directory| Ok(Entry::opened(directory)?.identity))
            .transpose()
    }

    fn check_current(&self) -> Result<(), ErasureFilesError> {
        let (directory, exists) = existing_directory(&self.source)?;
        match (exists, &self.directory) {
            (false, None) => Ok(()),
            (true, Some(original)) => {
                if Entry::opened(original)? == Entry::opened(&directory)? {
                    Ok(())
                } else {
                    Err(ErasureFilesError::UnsafePath)
                }
            }
            _ => Err(ErasureFilesError::UnsafePath),
        }
    }

    fn remove(&self, protected: &[Option<Identity>; 4]) -> Result<(), ErasureFilesError> {
        let Some(directory) = &self.directory else {
            return Ok(());
        };
        let parent = open_parent(&self.path)?;
        let name = self.path.file_name().ok_or(ErasureFilesError::UnsafePath)?;
        let before = Entry::opened(directory)?;
        verify_entry(&parent, name, &before)?;
        walk(directory, before.mount, protected, true)?;
        verify_entry(&parent, name, &before)?;
        // Compose owns the mount point. Keep the emptied directory attached.
        if before.mount == Entry::opened(&parent)?.mount {
            fs::unlinkat(&parent, name, AtFlags::REMOVEDIR)?;
            fs::fsync(parent)?;
        }
        Ok(())
    }
}

fn protected(path: &Path) -> Result<PathBuf, ErasureFilesError> {
    resolve_protected_root(path).map_err(|_| ErasureFilesError::UnsafePath)
}

fn open_parent(path: &Path) -> Result<OwnedFd, ErasureFilesError> {
    let parent = path.parent().ok_or(ErasureFilesError::UnsafePath)?;
    let (directory, exists) = existing_directory(parent)?;
    if !exists {
        return Err(ErasureFilesError::UnsafePath);
    }
    Ok(directory)
}

fn verify_entry(
    parent: &OwnedFd,
    name: impl Arg + Copy,
    opened: &Entry,
) -> Result<(), ErasureFilesError> {
    if *opened != Entry::lookup(parent, name)? {
        return Err(ErasureFilesError::UnsafePath);
    }
    Ok(())
}

fn walk(
    root: &OwnedFd,
    mount: u64,
    protected: &[Option<Identity>; 4],
    remove: bool,
) -> Result<(), ErasureFilesError> {
    for entry in Dir::read_from(root)? {
        let entry = entry?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        let entry = Entry::lookup(root, name)?;
        if entry.mount != mount {
            return Err(ErasureFilesError::NestedMount);
        }
        if protected.contains(&Some(entry.identity)) {
            return Err(ErasureFilesError::Overlap);
        }
        let flags = match entry.kind {
            FileType::Directory => {
                let child = fs::openat(root, name, DIRECTORY_READ, Mode::empty())?;
                if entry != Entry::opened(&child)? {
                    return Err(ErasureFilesError::UnsafePath);
                }
                walk(&child, mount, protected, remove)?;
                AtFlags::REMOVEDIR
            }
            FileType::Symlink => return Err(ErasureFilesError::UnsafePath),
            _ => AtFlags::empty(),
        };
        if remove {
            verify_entry(root, name, &entry)?;
            fs::unlinkat(root, name, flags)?;
        }
    }
    if remove {
        fs::fsync(root)?;
    }
    Ok(())
}
