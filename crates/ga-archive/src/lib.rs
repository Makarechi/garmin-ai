pub mod backup_space;
mod key;
mod staging;
mod token_file;

pub mod backup_bundle;
pub mod backup_envelope;

use std::{
    env,
    error::Error,
    ffi::OsStr,
    fmt,
    fs::File,
    io::{self, Read, Seek, Write},
    os::fd::OwnedFd,
    path::{Component, Path},
};

use ga_domain::legacy_json::{self, JsonError, Utf8Layout};
use rustix::{
    fs::{self, AtFlags, FileType, Mode, OFlags},
    io::Errno,
};
use sha2::{Digest, Sha256};
use staging::Stage;

pub use key::{ArchiveKey, ArchiveKind, InvalidArchiveKey};
pub use token_file::{GarminTokenFile, GarminTokenGuard};

const BUFFER_SIZE: usize = 64 * 1024;
const DIRECTORY_MODE: Mode = Mode::RWXU;
const FILE_MODE: Mode = Mode::RUSR.union(Mode::WUSR);
const DIRECTORY_READ: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

pub struct Archive {
    root: OwnedFd,
    writable: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct PutLimits {
    pub max_bytes: u64,
    pub disk_reserve_bytes: u64,
}

#[derive(Debug)]
pub enum ArchiveError {
    InvalidRoot,
    InvalidObject,
    ExistingObjectDigestMismatch,
    ReadCancelled,
    ReadOnly,
    PayloadTooLarge,
    InsufficientSpace,
    StagingEntropy,
    StagingBusy,
    Json(JsonError),
    Io(io::Error),
}

impl fmt::Display for ArchiveError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidRoot => "Invalid archive root",
            Self::InvalidObject => "Archive object must be a regular file",
            Self::ExistingObjectDigestMismatch => "Existing archive object has a different digest",
            Self::ReadCancelled => "Archive verification was cancelled",
            Self::ReadOnly => "Archive is open for read-only access",
            Self::PayloadTooLarge => "Archive input exceeds the byte limit",
            Self::InsufficientSpace => "Available archive space is below the required limit",
            Self::StagingEntropy => "Cannot allocate a private archive staging name",
            Self::StagingBusy => "Archive staging is busy",
            Self::Json(_) => "JSON archive serialization failed",
            Self::Io(_) => "Archive filesystem operation failed",
        })
    }
}

impl Error for ArchiveError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Json(error) => Some(error),
            _ => None,
        }
    }
}

impl From<Errno> for ArchiveError {
    fn from(error: Errno) -> Self {
        io::Error::from_raw_os_error(error.raw_os_error()).into()
    }
}

impl From<io::Error> for ArchiveError {
    fn from(error: io::Error) -> Self {
        if error.kind() == io::ErrorKind::StorageFull
            || error.raw_os_error() == Some(Errno::DQUOT.raw_os_error())
        {
            Self::InsufficientSpace
        } else {
            Self::Io(error)
        }
    }
}

impl Archive {
    pub fn open(path: &Path) -> Result<Self, ArchiveError> {
        Ok(Self {
            root: open_private_root(path)?,
            writable: true,
        })
    }

    /// Open an existing private archive without creating or changing entries.
    pub fn open_read_only(path: &Path) -> Result<Self, ArchiveError> {
        Ok(Self {
            root: open_root(path, false)?,
            writable: false,
        })
    }

    pub fn open_reader(&self, key: &ArchiveKey) -> Result<File, ArchiveError> {
        let (prefix, name) = key.path_parts();
        let directory = fs::openat(&self.root, prefix, DIRECTORY_READ, Mode::empty())?;
        open_regular(&directory, &name)
    }

    /// Verify a private archived object in bounded chunks and rewind its open handle.
    pub fn open_verified_reader(
        &self,
        key: &ArchiveKey,
        max_bytes: u64,
        cancelled: impl Fn() -> bool,
    ) -> Result<File, ArchiveError> {
        if cancelled() {
            return Err(ArchiveError::ReadCancelled);
        }
        let mut file = self.open_reader(key)?;
        let bytes = file.metadata()?.len();
        if bytes > max_bytes {
            return Err(ArchiveError::PayloadTooLarge);
        }
        let mut buffer = [0; BUFFER_SIZE];
        verify_digest(&mut file, key, &mut buffer, bytes, cancelled)?;
        file.rewind()?;
        Ok(file)
    }

    pub fn put_json(&self, payload: &[u8], limits: PutLimits) -> Result<ArchiveKey, ArchiveError> {
        self.require_writable()?;
        let max_bytes = usize::try_from(limits.max_bytes).unwrap_or(usize::MAX);
        let encoded = legacy_json::encode(payload, Utf8Layout::Archive, max_bytes)
            .map_err(ArchiveError::Json)?;
        self.put_reader(encoded.as_slice(), ArchiveKind::Json, limits)
    }

    /// Remove abandoned Rust staging entries while all writers are excluded.
    pub fn remove_orphaned_staging(&self) -> Result<usize, ArchiveError> {
        self.require_writable()?;
        staging::remove_orphans(&self.root)
    }

    /// Stage and hash a stream in fixed-size chunks before publishing its key.
    pub fn put_reader(
        &self,
        mut source: impl Read,
        kind: ArchiveKind,
        limits: PutLimits,
    ) -> Result<ArchiveKey, ArchiveError> {
        self.require_writable()?;
        self.check_space(limits.disk_reserve_bytes, 0)?;
        let _publication = staging::lock_for_write(&self.root)?;
        let mut stage = Stage::create(&self.root)?;
        let mut hasher = Sha256::new();
        let mut buffer = [0; BUFFER_SIZE];
        let mut total = 0_u64;
        loop {
            let count = match source.read(&mut buffer) {
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                result => result?,
            };
            if count == 0 {
                break;
            }
            total = total
                .checked_add(count as u64)
                .ok_or(ArchiveError::PayloadTooLarge)?;
            if total > limits.max_bytes {
                return Err(ArchiveError::PayloadTooLarge);
            }
            self.check_space(limits.disk_reserve_bytes, count as u64)?;
            stage.file.write_all(&buffer[..count])?;
            hasher.update(&buffer[..count]);
        }
        stage.file.sync_all()?;

        let key = ArchiveKey {
            digest: hasher.finalize().into(),
            kind,
        };
        let (prefix, name) = key.path_parts();
        let directory = ensure_directory(&self.root, OsStr::new(&prefix))?;
        fs::fchmod(&directory, DIRECTORY_MODE)?;
        match fs::linkat(
            &self.root,
            stage.name.as_str(),
            &directory,
            name.as_str(),
            AtFlags::empty(),
        ) {
            Ok(()) => {}
            Err(Errno::EXIST) => verify_existing(&directory, &name, &key, &mut buffer, total)?,
            Err(error) => return Err(error.into()),
        }
        fs::fsync(&directory)?;
        Ok(key)
    }

    fn require_writable(&self) -> Result<(), ArchiveError> {
        if self.writable {
            Ok(())
        } else {
            Err(ArchiveError::ReadOnly)
        }
    }

    fn check_space(&self, reserve: u64, next_bytes: u64) -> Result<(), ArchiveError> {
        let stat = fs::fstatvfs(&self.root)?;
        let available = stat.f_bavail.saturating_mul(stat.f_frsize);
        if available.saturating_sub(reserve) < next_bytes || available < reserve {
            return Err(ArchiveError::InsufficientSpace);
        }
        Ok(())
    }
}

fn open_private_root(path: &Path) -> Result<OwnedFd, ArchiveError> {
    open_root(path, true)
}

fn open_root(path: &Path, writable: bool) -> Result<OwnedFd, ArchiveError> {
    if path.as_os_str().is_empty() {
        return Err(ArchiveError::InvalidRoot);
    }
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()?.join(path)
    };
    if path
        .components()
        .any(|part| matches!(part, Component::ParentDir))
        || path.parent().is_none()
    {
        return Err(ArchiveError::InvalidRoot);
    }
    let mut directory = fs::open("/", DIRECTORY_READ, Mode::empty())?;
    for part in path.components() {
        if let Component::Normal(name) = part {
            directory = if writable {
                ensure_directory(&directory, name)?
            } else {
                fs::openat(&directory, name, DIRECTORY_READ, Mode::empty())?
            };
        }
    }
    if writable {
        fs::fchmod(&directory, DIRECTORY_MODE)?;
        fs::fsync(&directory)?;
    } else {
        let mode = Mode::from_raw_mode(fs::fstat(&directory)?.st_mode);
        if mode.intersects(Mode::RWXG | Mode::RWXO) {
            return Err(ArchiveError::InvalidRoot);
        }
    }
    Ok(directory)
}

fn ensure_directory(parent: &OwnedFd, name: &OsStr) -> Result<OwnedFd, ArchiveError> {
    match fs::mkdirat(parent, name, DIRECTORY_MODE) {
        Ok(()) | Err(Errno::EXIST) => {}
        Err(error) => return Err(error.into()),
    }
    let directory = fs::openat(parent, name, DIRECTORY_READ, Mode::empty())?;
    fs::fsync(parent)?;
    Ok(directory)
}

fn open_regular(directory: &OwnedFd, name: impl AsRef<OsStr>) -> Result<File, ArchiveError> {
    let name = name.as_ref();
    let metadata = fs::statat(directory, name, AtFlags::SYMLINK_NOFOLLOW)?;
    if !FileType::from_raw_mode(metadata.st_mode).is_file() {
        return Err(ArchiveError::InvalidObject);
    }
    let file = File::from(fs::openat(
        directory,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::NOCTTY | OFlags::CLOEXEC,
        Mode::empty(),
    )?);
    if !file.metadata()?.is_file() {
        return Err(ArchiveError::InvalidObject);
    }
    Ok(file)
}

fn verify_existing(
    directory: &OwnedFd,
    name: &str,
    key: &ArchiveKey,
    buffer: &mut [u8],
    expected_bytes: u64,
) -> Result<(), ArchiveError> {
    let mut file = open_regular(directory, name)?;
    if file.metadata()?.len() != expected_bytes {
        return Err(ArchiveError::ExistingObjectDigestMismatch);
    }
    verify_digest(&mut file, key, buffer, expected_bytes, || false)
}

fn verify_digest(
    file: &mut File,
    key: &ArchiveKey,
    buffer: &mut [u8],
    expected_bytes: u64,
    cancelled: impl Fn() -> bool,
) -> Result<(), ArchiveError> {
    let mut source = file.take(expected_bytes.saturating_add(1));
    let mut hasher = Sha256::new();
    let mut observed_bytes = 0;
    loop {
        if cancelled() {
            return Err(ArchiveError::ReadCancelled);
        }
        let count = match source.read(buffer) {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => result?,
        };
        if count == 0 {
            break;
        }
        observed_bytes += count as u64;
        hasher.update(&buffer[..count]);
    }
    let observed: [u8; 32] = hasher.finalize().into();
    if observed_bytes != expected_bytes || observed != key.digest {
        return Err(ArchiveError::ExistingObjectDigestMismatch);
    }
    Ok(())
}
