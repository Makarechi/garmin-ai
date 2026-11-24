//! Stream the authenticated backup envelope used by the Python recovery tools.

use std::{
    env,
    error::Error,
    ffi::OsStr,
    fmt,
    fs::File,
    io::{self, Read, Seek, SeekFrom, Write},
    os::{fd::OwnedFd, unix::fs::MetadataExt},
    path::{Component, Path},
};

use openssl::symm::{Cipher, Crypter, Mode as CipherMode};
use rustix::{
    fs::{self, AtFlags, FileType, Mode, OFlags, RenameFlags},
    io::Errno,
    process::geteuid,
};
use sha2::{Digest, Sha256};

use crate::{ArchiveError, DIRECTORY_READ, staging::Stage, storage_files::StorageFilesError};

/// Format identifier at the start of each encrypted backup.
pub const FORMAT: &str = "GARMINAI1";
const MAGIC: &[u8] = FORMAT.as_bytes();
const NONCE_BYTES: usize = 12;
const TAG_BYTES: usize = 16;
const HEADER_BYTES: u64 = (MAGIC.len() + NONCE_BYTES) as u64;
const FRAME_BYTES: u64 = HEADER_BYTES + TAG_BYTES as u64;
const CHUNK: usize = 64 * 1024;
const MAX_PLAINTEXT: u64 = (1_u64 << 36) - 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BackupDigest {
    pub bytes: u64,
    pub sha256: [u8; 32],
}

#[derive(Debug)]
pub enum BackupError {
    InvalidFrame,
    Authentication,
    Cipher,
    TooLarge,
    InvalidPath,
    Exists,
    Entropy,
    Workspace(StorageFilesError),
    Filesystem(ArchiveError),
    Io(io::Error),
}

impl fmt::Display for BackupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidFrame => "Invalid encrypted backup",
            Self::Authentication => "Backup authentication failed",
            Self::Cipher => "Backup encryption could not be completed",
            Self::TooLarge => "Backup exceeds the authenticated format limit",
            Self::InvalidPath | Self::Workspace(StorageFilesError::Unsafe) => {
                "Backup path is invalid or redirects"
            }
            Self::Exists => "Backup destination already exists",
            Self::Entropy => "Backup nonce could not be generated",
            Self::Workspace(StorageFilesError::Occupied) => {
                "Backup recovery is already in progress"
            }
            Self::Workspace(_) | Self::Filesystem(_) | Self::Io(_) => {
                "Backup filesystem operation failed"
            }
        })
    }
}
impl Error for BackupError {}
impl From<ArchiveError> for BackupError {
    fn from(error: ArchiveError) -> Self {
        Self::Filesystem(error)
    }
}
impl From<io::Error> for BackupError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}
impl From<Errno> for BackupError {
    fn from(error: Errno) -> Self {
        Self::Filesystem(error.into())
    }
}

/// Stream one backup, authenticate the staged ciphertext, and publish it without replacement.
pub fn write_encrypted_backup(
    destination: &Path,
    key: &[u8; 32],
    build: impl FnOnce(&mut dyn Write) -> io::Result<()>,
) -> Result<BackupDigest, BackupError> {
    write_encrypted_backup_guarded(destination, key, build, || Ok(()))
}

pub(crate) fn write_encrypted_backup_guarded(
    destination: &Path,
    key: &[u8; 32],
    build: impl FnOnce(&mut dyn Write) -> io::Result<()>,
    before_publish: impl FnOnce() -> io::Result<()>,
) -> Result<BackupDigest, BackupError> {
    write_destination(destination, true, |file| {
        let mut nonce = [0; NONCE_BYTES];
        getrandom::fill(&mut nonce).map_err(|_| BackupError::Entropy)?;
        let mut cipher = CipherWriter::new(file, key, &nonce)?;
        build(&mut cipher)?;
        let expected = cipher.finish()?;
        file.sync_all()?;
        let verified = read_authenticated(file, key, &mut io::sink())?;
        if expected != verified {
            return Err(BackupError::Authentication);
        }
        before_publish()?;
        Ok(verified)
    })
}

/// Verify a legacy ciphertext without creating a plaintext file.
pub fn verify_encrypted_backup(source: &Path, key: &[u8; 32]) -> Result<BackupDigest, BackupError> {
    authenticate_source(source, key, &mut io::sink())
}

/// Decrypt to a private staging file. Only publish after the entire ciphertext authenticates.
pub fn decrypt_encrypted_backup(
    source: &Path,
    destination: &Path,
    key: &[u8; 32],
) -> Result<BackupDigest, BackupError> {
    write_destination(destination, false, |file| {
        authenticate_source(source, key, file)
    })
}

pub(crate) fn authenticate_source(
    path: &Path,
    key: &[u8; 32],
    plain: &mut dyn Write,
) -> Result<BackupDigest, BackupError> {
    authenticate_open_source(path, open_source(path)?, key, plain)
}

pub(crate) fn authenticate_open_source(
    path: &Path,
    mut file: File,
    key: &[u8; 32],
    plain: &mut dyn Write,
) -> Result<BackupDigest, BackupError> {
    let original = revision(&file)?;
    let result = read_authenticated(&mut file, key, plain)?;
    if revision(&file)? != original || revision(&open_source(path)?)? != original {
        return Err(BackupError::InvalidFrame);
    }
    Ok(result)
}

#[derive(PartialEq, Eq)]
pub(crate) struct Revision {
    device: u64,
    inode: u64,
    pub(crate) bytes: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}

pub(crate) fn revision(file: &File) -> Result<Revision, BackupError> {
    let metadata = file.metadata()?;
    Ok(Revision {
        device: metadata.dev(),
        inode: metadata.ino(),
        bytes: metadata.len(),
        modified: (metadata.mtime(), metadata.mtime_nsec()),
        changed: (metadata.ctime(), metadata.ctime_nsec()),
    })
}

fn new_cipher(
    mode: CipherMode,
    key: &[u8; 32],
    nonce: &[u8; NONCE_BYTES],
) -> Result<Crypter, BackupError> {
    let mut cipher = Crypter::new(Cipher::aes_256_gcm(), mode, key, Some(nonce))
        .map_err(|_| BackupError::Cipher)?;
    cipher.pad(false);
    cipher.aad_update(MAGIC).map_err(|_| BackupError::Cipher)?;
    Ok(cipher)
}

struct CipherWriter<'a> {
    output: &'a mut File,
    cipher: Crypter,
    digest: Sha256,
    bytes: u64,
    failed: bool,
    block: Box<[u8]>,
}

impl<'a> CipherWriter<'a> {
    fn new(
        output: &'a mut File,
        key: &[u8; 32],
        nonce: &[u8; NONCE_BYTES],
    ) -> Result<Self, BackupError> {
        let cipher = new_cipher(CipherMode::Encrypt, key, nonce)?;
        output.write_all(MAGIC)?;
        output.write_all(nonce)?;
        Ok(Self {
            output,
            cipher,
            digest: Sha256::new(),
            bytes: 0,
            failed: false,
            block: vec![0; CHUNK + 16].into_boxed_slice(),
        })
    }

    fn finish(mut self) -> Result<BackupDigest, BackupError> {
        if self.failed {
            return Err(BackupError::Cipher);
        }
        let last = self
            .cipher
            .finalize(&mut self.block)
            .map_err(|_| BackupError::Cipher)?;
        self.output.write_all(&self.block[..last])?;
        let mut tag = [0; TAG_BYTES];
        self.cipher
            .get_tag(&mut tag)
            .map_err(|_| BackupError::Cipher)?;
        self.output.write_all(&tag)?;
        Ok(BackupDigest {
            bytes: self.bytes,
            sha256: self.digest.finalize().into(),
        })
    }

    fn write_chunk(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let bytes = &bytes[..bytes.len().min(CHUNK)];
        if self.bytes + bytes.len() as u64 > MAX_PLAINTEXT {
            return Err(io::Error::other(
                "Backup exceeds the authenticated format limit",
            ));
        }
        let count = self
            .cipher
            .update(bytes, &mut self.block)
            .map_err(|_| io::Error::other("Backup encryption could not be completed"))?;
        self.output.write_all(&self.block[..count])?;
        self.digest.update(bytes);
        self.bytes += bytes.len() as u64;
        Ok(bytes.len())
    }
}

impl Write for CipherWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.failed {
            return Err(io::Error::other("Backup encryption could not be completed"));
        }
        let written = self.write_chunk(bytes);
        self.failed = written.is_err();
        written
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.failed {
            return Err(io::Error::other("Backup encryption could not be completed"));
        }
        let flushed = self.output.flush();
        self.failed = flushed.is_err();
        flushed
    }
}

fn read_authenticated(
    source: &mut File,
    key: &[u8; 32],
    plain: &mut dyn Write,
) -> Result<BackupDigest, BackupError> {
    let original = revision(source)?;
    let size = original.bytes;
    let length = size
        .checked_sub(FRAME_BYTES)
        .ok_or(BackupError::InvalidFrame)?;
    if length > MAX_PLAINTEXT {
        return Err(BackupError::TooLarge);
    }
    source.rewind()?;
    let mut magic = [0; MAGIC.len()];
    read_frame(source, &mut magic)?;
    if magic != MAGIC {
        return Err(BackupError::InvalidFrame);
    }
    let mut nonce = [0; NONCE_BYTES];
    read_frame(source, &mut nonce)?;
    source.seek(SeekFrom::Start(size - TAG_BYTES as u64))?;
    let mut tag = [0; TAG_BYTES];
    read_frame(source, &mut tag)?;
    source.seek(SeekFrom::Start(HEADER_BYTES))?;

    let mut cipher = new_cipher(CipherMode::Decrypt, key, &nonce)?;
    cipher
        .set_tag(&tag)
        .map_err(|_| BackupError::InvalidFrame)?;
    let mut remaining = length;
    let mut digest = Sha256::new();
    let mut input = vec![0; CHUNK].into_boxed_slice();
    let mut output = vec![0; CHUNK + 16].into_boxed_slice();
    while remaining != 0 {
        let bytes = usize::try_from(remaining).unwrap_or(CHUNK).min(CHUNK);
        read_frame(source, &mut input[..bytes])?;
        let returned = cipher
            .update(&input[..bytes], &mut output)
            .map_err(|_| BackupError::Authentication)?;
        plain.write_all(&output[..returned])?;
        digest.update(&output[..returned]);
        remaining -= bytes as u64;
    }
    let tail = cipher
        .finalize(&mut output)
        .map_err(|_| BackupError::Authentication)?;
    plain.write_all(&output[..tail])?;
    digest.update(&output[..tail]);
    let mut current_tag = [0; TAG_BYTES];
    read_frame(source, &mut current_tag)?;
    let mut extra = [0; 1];
    if tag != current_tag || source.read(&mut extra)? != 0 || revision(source)? != original {
        return Err(BackupError::InvalidFrame);
    }
    Ok(BackupDigest {
        bytes: length,
        sha256: digest.finalize().into(),
    })
}

fn read_frame(source: &mut File, data: &mut [u8]) -> Result<(), BackupError> {
    source.read_exact(data).map_err(|error| {
        if error.kind() == io::ErrorKind::UnexpectedEof {
            BackupError::InvalidFrame
        } else {
            error.into()
        }
    })
}

pub fn open_source(path: &Path) -> Result<File, BackupError> {
    let (directory, name) = parent_file(path, false)?;
    let metadata = fs::statat(&directory, name, AtFlags::SYMLINK_NOFOLLOW)?;
    if FileType::from_raw_mode(metadata.st_mode) != FileType::RegularFile {
        return Err(BackupError::InvalidPath);
    }
    let file = File::from(fs::openat(
        &directory,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::NOCTTY | OFlags::CLOEXEC,
        Mode::empty(),
    )?);
    if !file.metadata()?.is_file() {
        return Err(BackupError::InvalidPath);
    }
    Ok(file)
}

fn open_destination(path: &Path) -> Result<(OwnedFd, &OsStr), BackupError> {
    let (directory, name) = parent_file(path, true)?;
    match fs::statat(&directory, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(_) => Err(BackupError::Exists),
        Err(Errno::NOENT) => Ok((directory, name)),
        Err(error) => Err(error.into()),
    }
}

pub(crate) fn parent_file(path: &Path, create: bool) -> Result<(OwnedFd, &OsStr), BackupError> {
    let name = path.file_name().ok_or(BackupError::InvalidPath)?;
    let parent = path.parent().ok_or(BackupError::InvalidPath)?;
    let absolute = if parent.is_absolute() {
        parent.to_path_buf()
    } else {
        env::current_dir()?.join(parent)
    };
    let mut directory = fs::open("/", DIRECTORY_READ, Mode::empty())?;
    for component in absolute.components() {
        let name = match component {
            Component::Normal(name) => name,
            Component::ParentDir => OsStr::new(".."),
            _ => continue,
        };
        directory = match fs::openat(&directory, name, DIRECTORY_READ, Mode::empty()) {
            Ok(next) => next,
            Err(Errno::NOENT) if create => {
                match fs::mkdirat(&directory, name, Mode::RWXU) {
                    Ok(()) => fs::fsync(&directory)?,
                    Err(Errno::EXIST) => {}
                    Err(error) => return Err(error.into()),
                }
                fs::openat(&directory, name, DIRECTORY_READ, Mode::empty())?
            }
            Err(Errno::LOOP | Errno::NOTDIR) => return Err(BackupError::InvalidPath),
            Err(error) => return Err(error.into()),
        };
    }
    Ok((directory, name))
}

pub fn write_private_export<T>(
    destination: &Path,
    write: impl FnOnce(&mut File) -> Result<T, BackupError>,
) -> Result<T, BackupError> {
    write_destination(destination, false, write)
}

fn write_destination<T>(
    destination: &Path,
    readable: bool,
    write: impl FnOnce(&mut File) -> Result<T, BackupError>,
) -> Result<T, BackupError> {
    let (parent, name) = open_destination(destination)?;
    let directory = PrivateStageDirectory::create(&parent)?;
    let mut stage = if readable {
        Stage::create_readable(&directory.handle)?
    } else {
        Stage::create(&directory.handle)?
    };
    let result = write(&mut stage.file)?;
    stage.file.sync_all()?;
    let visible = File::from(fs::openat(
        &directory.handle,
        stage.name.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::NOCTTY | OFlags::CLOEXEC,
        Mode::empty(),
    )?);
    if revision(&visible)? != revision(&stage.file)? {
        return Err(BackupError::InvalidPath);
    }
    match fs::linkat(
        &directory.handle,
        stage.name.as_str(),
        &parent,
        name,
        AtFlags::empty(),
    ) {
        Ok(()) => {}
        Err(Errno::EXIST) => return Err(BackupError::Exists),
        Err(Errno::OPNOTSUPP | Errno::NOSYS | Errno::PERM) => {
            match fs::renameat_with(
                &directory.handle,
                stage.name.as_str(),
                &parent,
                name,
                RenameFlags::NOREPLACE,
            ) {
                Ok(()) => {}
                Err(Errno::EXIST | Errno::NOTEMPTY) => return Err(BackupError::Exists),
                Err(error) => return Err(error.into()),
            }
        }
        Err(error) => return Err(error.into()),
    }
    drop(stage);
    drop(directory);
    fs::fsync(parent)?;
    Ok(result)
}

pub(crate) struct PrivateStageDirectory<'a> {
    parent: &'a OwnedFd,
    name: String,
    pub(crate) handle: OwnedFd,
}

impl<'a> PrivateStageDirectory<'a> {
    pub(crate) fn create(parent: &'a OwnedFd) -> Result<Self, BackupError> {
        let mut random = [0; 16];
        getrandom::fill(&mut random).map_err(|_| BackupError::Entropy)?;
        let name = format!(".rust-backup-{}", hex::encode(random));
        fs::mkdirat(parent, name.as_str(), Mode::RWXU)?;
        let handle = fs::openat(parent, name.as_str(), DIRECTORY_READ, Mode::empty())?;
        let staging = Self {
            parent,
            name,
            handle,
        };
        let stat = fs::fstat(&staging.handle)?;
        if stat.st_uid != geteuid().as_raw()
            || Mode::from_raw_mode(stat.st_mode).intersects(Mode::RWXG | Mode::RWXO)
        {
            return Err(BackupError::InvalidPath);
        }
        Ok(staging)
    }
}

impl Drop for PrivateStageDirectory<'_> {
    fn drop(&mut self) {
        if let (Ok(handle), Ok(visible)) = (
            fs::fstat(&self.handle),
            fs::statat(self.parent, self.name.as_str(), AtFlags::SYMLINK_NOFOLLOW),
        ) && handle.st_uid == geteuid().as_raw()
            && (handle.st_dev, handle.st_ino) == (visible.st_dev, visible.st_ino)
        {
            let _ = fs::unlinkat(self.parent, self.name.as_str(), AtFlags::REMOVEDIR);
        }
    }
}
