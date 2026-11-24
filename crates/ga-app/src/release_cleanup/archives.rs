use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::os::unix::ffi::OsStrExt;
use std::path::{Component, Path};
use std::time::{Duration, SystemTime};

use flate2::read::GzDecoder;
use rustix::fs::{self, AtFlags, Mode, OFlags, Stat};
use rustix::io::Errno;
use serde::Deserialize;

use crate::{Failure, Result};

const DIRECTORY: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
const READ_ARCHIVE: OFlags = OFlags::RDONLY
    .union(OFlags::NOFOLLOW)
    .union(OFlags::NONBLOCK)
    .union(OFlags::CLOEXEC);
const ARCHIVE_AGE: Duration = Duration::from_hours(24);

pub(crate) fn open_upload(path: &Path) -> Result<Option<File>> {
    let invalid = || {
        Failure::message("Archive directories must be canonical upload paths outside private data")
    };
    let mut canonical = Path::new("/").to_owned();
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name)
                if ![b"backup".as_slice(), b"backups", b"data", b"tokens"]
                    .iter()
                    .any(|forbidden| name.as_bytes().eq_ignore_ascii_case(forbidden)) =>
            {
                canonical.push(name);
            }
            _ => return Err(invalid()),
        }
    }
    if canonical.as_os_str() != path.as_os_str() {
        return Err(invalid());
    }

    // Walk from a trusted root and keep using the open directory even if its
    // pathname is replaced during inspection.
    let mut directory = fs::open("/", DIRECTORY, Mode::empty())?;
    for part in path.iter().skip(1) {
        directory = match fs::openat(&directory, part, DIRECTORY, Mode::empty()) {
            Ok(next) => next,
            Err(Errno::NOENT) => return Ok(None),
            Err(Errno::LOOP | Errno::NOTDIR) => return Err(invalid()),
            Err(error) => return Err(error.into()),
        };
    }
    Ok(Some(directory.into()))
}

fn matches_name(name: &OsStr) -> bool {
    name.as_bytes()
        .strip_prefix(b"garmin-ai-")
        .and_then(|name| name.strip_suffix(b".tar.gz"))
        .is_some_and(|tag| {
            !tag.is_empty()
                && tag
                    .iter()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-'))
        })
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ManifestEntry {
    #[serde(rename = "Config")]
    _config: String,
    #[serde(rename = "Layers")]
    _layers: Vec<serde::de::IgnoredAny>,
    repo_tags: Vec<String>,
}

fn image_archive(file: &File) -> std::result::Result<bool, std::io::Error> {
    let mut archive = tar::Archive::new(GzDecoder::new(file));
    // Docker writes this entry under its literal short name. Reading raw
    // headers avoids allocating arbitrary PAX or GNU long-name payloads.
    for entry in archive.entries()?.raw(true) {
        let mut entry = entry?;
        if entry.path_bytes().as_ref() != b"manifest.json" {
            continue;
        }
        if !entry.header().entry_type().is_file() || entry.size() > 1024 * 1024 {
            return Ok(false);
        }
        let manifest: Vec<ManifestEntry> = serde_json::from_reader(&mut entry)?;
        return Ok(!manifest.is_empty()
            && manifest.iter().all(|image| {
                !image.repo_tags.is_empty()
                    && image
                        .repo_tags
                        .iter()
                        .all(|tag| tag.starts_with("garmin-ai:"))
            }));
    }
    Ok(false)
}

fn regular_archive(info: &Stat) -> bool {
    fs::FileType::from_raw_mode(info.st_mode) == fs::FileType::RegularFile && info.st_nlink == 1
}

fn same_payload(actual: &Stat, expected: &Stat) -> bool {
    regular_archive(actual)
        && actual.st_dev == expected.st_dev
        && actual.st_ino == expected.st_ino
        && actual.st_size == expected.st_size
        && actual.st_mtime == expected.st_mtime
        && actual.st_mtime_nsec == expected.st_mtime_nsec
}

fn same_snapshot(actual: &Stat, expected: &Stat) -> bool {
    same_payload(actual, expected)
        && actual.st_ctime == expected.st_ctime
        && actual.st_ctime_nsec == expected.st_ctime_nsec
}

pub(crate) struct Candidate {
    pub name: OsString,
    pub bytes: u64,
    expected: Stat,
}

pub(crate) fn candidates(directory: &File, now: SystemTime) -> Result<Vec<Candidate>> {
    let mut candidates = Vec::new();
    for entry in fs::Dir::read_from(directory)? {
        let entry = entry?;
        let name = OsStr::from_bytes(entry.file_name().to_bytes());
        if !matches_name(name) {
            continue;
        }
        let stat = match fs::statat(directory, name, AtFlags::SYMLINK_NOFOLLOW) {
            Ok(stat) => stat,
            Err(Errno::NOENT) => continue,
            Err(error) => return Err(error.into()),
        };
        if !regular_archive(&stat) {
            continue;
        }
        let file: File = match fs::openat(directory, name, READ_ARCHIVE, Mode::empty()) {
            Ok(file) => file.into(),
            Err(Errno::NOENT | Errno::LOOP) => continue,
            Err(error) => return Err(error.into()),
        };
        let info = fs::fstat(&file)?;
        let metadata = file.metadata()?;
        if !same_snapshot(&info, &stat)
            || !now
                .duration_since(metadata.modified()?)
                .is_ok_and(|age| age >= ARCHIVE_AGE)
        {
            continue;
        }
        if !image_archive(&file).unwrap_or(false) {
            continue;
        }
        let after = fs::fstat(&file)?;
        if same_snapshot(&after, &info) {
            candidates.push(Candidate {
                name: name.to_owned(),
                bytes: metadata.len(),
                expected: info,
            });
        }
    }
    candidates.sort_unstable_by(|a, b| a.name.cmp(&b.name));
    Ok(candidates)
}

pub(crate) fn remove(directory: &File, root: &Path, candidate: &Candidate) -> Result<()> {
    let mut random = [0; 16];
    getrandom::fill(&mut random).map_err(|_| Failure::message("Archive isolation failed"))?;
    let private = format!(".garmin-ai-cleanup-{:032x}", u128::from_be_bytes(random));
    fs::mkdirat(directory, private.as_str(), Mode::RWXU)?;
    let result = (|| {
        let quarantine = fs::openat(directory, private.as_str(), DIRECTORY, Mode::empty())?;
        let before = fs::statat(directory, &candidate.name, AtFlags::SYMLINK_NOFOLLOW)?;
        if !same_snapshot(&before, &candidate.expected) {
            return Err(Failure::message("Archive changed during cleanup"));
        }
        fs::renameat(directory, &candidate.name, &quarantine, "archive")?;
        finish_isolated(directory, &quarantine, root.join(&private), candidate)
    })();

    // Remove only an empty directory. Any file kept for recovery stays private.
    match fs::unlinkat(directory, private.as_str(), AtFlags::REMOVEDIR) {
        Ok(()) | Err(Errno::NOTEMPTY | Errno::EXIST) => result,
        Err(error) => result.and(Err(error.into())),
    }
}

fn finish_isolated(
    directory: &File,
    quarantine: &rustix::fd::OwnedFd,
    recovery: std::path::PathBuf,
    candidate: &Candidate,
) -> Result<()> {
    let preserved = || {
        Failure(format!(
            "Archive cleanup failed; preserved for recovery at {}",
            recovery.join("archive").display()
        ))
    };
    // Keep using the already known archive inode. The rename changes ctime,
    // while an intervening upload also changes the inode, size, or mtime.
    let actual =
        fs::statat(quarantine, "archive", AtFlags::SYMLINK_NOFOLLOW).map_err(|_| preserved())?;
    let unchanged = same_payload(&actual, &candidate.expected);
    if !unchanged {
        // Re-publish without overwriting a second concurrent upload.
        fs::linkat(
            quarantine,
            "archive",
            directory,
            &candidate.name,
            AtFlags::empty(),
        )
        .map_err(|_| preserved())?;
    }
    fs::unlinkat(quarantine, "archive", AtFlags::empty()).map_err(|_| preserved())?;
    if !unchanged {
        return Err(Failure::message("Archive changed during cleanup"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs as disk;

    fn prepare(root: &Path) -> Result<(File, Candidate)> {
        let name = OsString::from("garmin-ai-synthetic.tar.gz");
        let path = root.join(&name);
        disk::write(&path, b"original")?;
        let expected = fs::statat(fs::CWD, &path, AtFlags::SYMLINK_NOFOLLOW)?;
        let file = File::open(root)?;
        Ok((
            file,
            Candidate {
                name,
                bytes: 8,
                expected,
            },
        ))
    }

    #[test]
    fn replacement_before_isolation_is_left_in_place() -> Result<()> {
        let root = tempfile::tempdir()?;
        let (directory, candidate) = prepare(root.path())?;
        let path = root.path().join(&candidate.name);
        disk::write(&path, b"replacement upload")?;

        let failure = remove(&directory, root.path(), &candidate).err();
        assert!(failure.is_some_and(|error| error.0.contains("Archive changed")));
        assert_eq!(disk::read(&path)?, b"replacement upload");
        assert_eq!(disk::read_dir(root.path())?.count(), 1);
        Ok(())
    }

    #[test]
    fn new_upload_after_isolation_survives() -> Result<()> {
        let root = tempfile::tempdir()?;
        let (directory, candidate) = prepare(root.path())?;
        let path = root.path().join(&candidate.name);
        let recovery = tempfile::tempdir_in(root.path())?;
        let quarantine = fs::open(recovery.path(), DIRECTORY, Mode::empty())?;
        fs::renameat(&directory, &candidate.name, &quarantine, "archive")?;
        disk::write(&path, b"new upload")?;

        finish_isolated(&directory, &quarantine, recovery.path().into(), &candidate)?;
        assert_eq!(disk::read(&path)?, b"new upload");
        assert!(!recovery.path().join("archive").exists());
        Ok(())
    }

    #[test]
    fn changed_upload_is_restored_when_original_name_is_free() -> Result<()> {
        let root = tempfile::tempdir()?;
        let (directory, candidate) = prepare(root.path())?;
        let path = root.path().join(&candidate.name);
        let recovery = tempfile::tempdir_in(root.path())?;
        let quarantine = fs::open(recovery.path(), DIRECTORY, Mode::empty())?;
        disk::write(&path, b"changed upload")?;
        fs::renameat(&directory, &candidate.name, &quarantine, "archive")?;

        let result = finish_isolated(&directory, &quarantine, recovery.path().into(), &candidate);
        assert!(result.is_err_and(|error| error.0.contains("Archive changed")));
        assert_eq!(disk::read(&path)?, b"changed upload");
        assert!(!recovery.path().join("archive").exists());
        Ok(())
    }

    #[test]
    fn two_concurrent_replacements_are_both_retained() -> Result<()> {
        let root = tempfile::tempdir()?;
        let (directory, candidate) = prepare(root.path())?;
        let path = root.path().join(&candidate.name);
        let recovery = tempfile::tempdir_in(root.path())?;
        let quarantine = fs::open(recovery.path(), DIRECTORY, Mode::empty())?;
        let incoming = root.path().join("incoming");
        disk::write(&incoming, b"first upload")?;
        disk::rename(incoming, &path)?;
        fs::renameat(&directory, &candidate.name, &quarantine, "archive")?;
        disk::write(&path, b"second upload")?;

        let result = finish_isolated(&directory, &quarantine, recovery.path().into(), &candidate);
        assert!(result.is_err_and(|error| error.0.contains("preserved for recovery")));
        assert_eq!(disk::read(&path)?, b"second upload");
        assert_eq!(
            disk::read(recovery.path().join("archive"))?,
            b"first upload"
        );
        Ok(())
    }
}
