//! Stream the current compressed backup inventory without modifying source files.

use std::{
    collections::BTreeMap,
    ffi::OsStr,
    fs::File,
    io::{self, Read, Write},
    os::{fd::OwnedFd, unix::ffi::OsStrExt},
    path::{Path, PathBuf},
};

use flate2::{Compression, write::GzEncoder};
use rustix::{
    fs::{self, AtFlags, Dir, FileType, Mode},
    io::Errno,
};
use sha2::{Digest, Sha256};
use tar::{Builder, Header};

use crate::{
    ArchiveError, ArchiveKey, DIRECTORY_READ,
    backup_envelope::{
        BackupDigest, BackupError, open_source, revision, write_encrypted_backup_guarded,
    },
    backup_space::existing_directory,
    open_regular,
    storage_path::{is_within, normalized},
};

const DATABASE_MEMBER: &str = "database.jsonl.gz";
const COVERAGE_MEMBER: &str = "coverage-report.json";
pub(crate) const MAX_DIRECTORY_DEPTH: usize = 128;

pub struct BackupBundle<'a> {
    pub data_dir: &'a Path,
    pub token_dir: &'a Path,
    pub database_export: &'a Path,
    pub required_raw: &'a [ArchiveKey],
}

/// Build a bundle from an already staged database export and one expected raw inventory.
/// The caller holds the storage and token coordination for the duration of the call.
pub fn write_backup_bundle(
    destination: &Path,
    key: &[u8; 32],
    bundle: BackupBundle<'_>,
) -> Result<BackupDigest, BackupError> {
    write_backup_bundle_guarded(destination, key, bundle, || Ok(()))
}

pub fn write_backup_bundle_guarded(
    destination: &Path,
    key: &[u8; 32],
    bundle: BackupBundle<'_>,
    before_publish: impl FnOnce() -> io::Result<()>,
) -> Result<BackupDigest, BackupError> {
    write_backup_bundle_cancellable(destination, key, bundle, || false, before_publish)
}

pub fn write_backup_bundle_cancellable(
    destination: &Path,
    key: &[u8; 32],
    bundle: BackupBundle<'_>,
    cancelled: impl Fn() -> bool,
    before_publish: impl FnOnce() -> io::Result<()>,
) -> Result<BackupDigest, BackupError> {
    let target = normalized(destination)?;
    if is_within(&target, &normalized(bundle.data_dir)?)?
        || is_within(&target, &normalized(bundle.token_dir)?)?
    {
        return Err(BackupError::InvalidPath);
    }
    let mut mandatory: BTreeMap<_, _> = bundle
        .required_raw
        .iter()
        .map(|key| (Path::new("raw").join(key.to_string()), key.digest))
        .collect();

    write_encrypted_backup_guarded(
        destination,
        key,
        |output| {
            let mut archive = Builder::new(CancellableWriter::new(
                GzEncoder::new(output, Compression::best()),
                &cancelled,
            ));
            let file = open_source(bundle.database_export).map_err(source_error)?;
            append_file(&mut archive, Path::new(DATABASE_MEMBER), file, None)?;
            let (data_dir, exists) = existing_directory(bundle.data_dir).map_err(archive_error)?;
            if exists {
                match fs::statat(&data_dir, COVERAGE_MEMBER, AtFlags::SYMLINK_NOFOLLOW) {
                    Ok(_) => {
                        let file =
                            open_regular(&data_dir, COVERAGE_MEMBER).map_err(archive_error)?;
                        append_file(&mut archive, Path::new(COVERAGE_MEMBER), file, None)?;
                    }
                    Err(Errno::NOENT) => {}
                    Err(error) => return Err(error.into()),
                }
            }
            for (path, prefix) in [
                (bundle.data_dir.join("raw"), "raw"),
                (bundle.token_dir.to_path_buf(), "tokens"),
            ] {
                let (root, exists) = existing_directory(&path).map_err(archive_error)?;
                if exists {
                    append_directory(
                        &mut archive,
                        &root,
                        &mut PathBuf::from(prefix),
                        &mut mandatory,
                        0,
                    )?;
                    let (current, exists) = existing_directory(&path).map_err(archive_error)?;
                    let before = fs::fstat(&root)?;
                    let after = fs::fstat(current)?;
                    if !exists || (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) {
                        return Err(changed());
                    }
                }
            }
            if !mandatory.is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "The snapshot references an archive file absent from the backup",
                ));
            }
            archive.into_inner()?.into_inner().finish()?;
            Ok(())
        },
        before_publish,
    )
}

/// Stop staging output once the caller loses its job or worker lease.
pub struct CancellableWriter<W, F> {
    writer: W,
    cancelled: F,
}

impl<W, F> CancellableWriter<W, F> {
    pub fn new(writer: W, cancelled: F) -> Self {
        Self { writer, cancelled }
    }

    fn into_inner(self) -> W {
        self.writer
    }
}

impl<W: Write, F: Fn() -> bool> Write for CancellableWriter<W, F> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if (self.cancelled)() {
            return Err(io::Error::other("Backup staging was cancelled"));
        }
        self.writer.write(bytes)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }
}

fn append_directory(
    archive: &mut Builder<impl Write>,
    directory: &OwnedFd,
    member: &mut PathBuf,
    mandatory: &mut BTreeMap<PathBuf, [u8; 32]>,
    depth: usize,
) -> io::Result<()> {
    if depth > MAX_DIRECTORY_DEPTH {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Backup tree is too deep",
        ));
    }
    let handle = File::from(rustix::io::dup(directory)?);
    let initial = revision(&handle).map_err(source_error)?;
    for entry in Dir::read_from(directory)? {
        let entry = entry?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        let path = OsStr::from_bytes(name.to_bytes());
        let stat = fs::statat(directory, name, AtFlags::SYMLINK_NOFOLLOW)?;
        member.push(path);
        match FileType::from_raw_mode(stat.st_mode) {
            FileType::Directory => {
                let child = fs::openat(directory, name, DIRECTORY_READ, Mode::empty())?;
                append_directory(archive, &child, member, mandatory, depth + 1)?;
            }
            FileType::RegularFile => {
                let file = open_regular(directory, path).map_err(archive_error)?;
                let expected = mandatory.remove(member.as_path());
                append_file(archive, member, file, expected.as_ref())?;
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Unsafe backup member",
                ));
            }
        }
        member.pop();
    }
    if revision(&handle).map_err(source_error)? != initial {
        return Err(changed());
    }
    Ok(())
}

fn append_file(
    archive: &mut Builder<impl Write>,
    member: &Path,
    mut input: File,
    expected: Option<&[u8; 32]>,
) -> io::Result<()> {
    let before = revision(&input).map_err(source_error)?;
    let mut header = Header::new_gnu();
    header.set_mode(0o600);
    header.set_size(before.bytes);
    header.set_entry_type(tar::EntryType::Regular);
    let mut reader = VerifiedReader {
        file: (&mut input).take(before.bytes),
        sha256: expected.map(|_| Sha256::new()),
    };
    archive.append_data(&mut header, member, &mut reader)?;
    if reader.file.limit() != 0
        || reader
            .sha256
            .zip(expected)
            .is_some_and(|(digest, expected)| {
                let observed: [u8; 32] = digest.finalize().into();
                &observed != expected
            })
        || revision(&input).map_err(source_error)? != before
    {
        return Err(changed());
    }
    Ok(())
}

struct VerifiedReader<R> {
    file: R,
    sha256: Option<Sha256>,
}

impl<R: Read> Read for VerifiedReader<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let bytes = self.file.read(buffer)?;
        if let Some(digest) = &mut self.sha256 {
            digest.update(&buffer[..bytes]);
        }
        Ok(bytes)
    }
}

fn changed() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "Backup source changed during capture",
    )
}

fn archive_error(error: ArchiveError) -> io::Error {
    io::Error::other(error)
}

fn source_error(error: BackupError) -> io::Error {
    io::Error::other(error)
}
