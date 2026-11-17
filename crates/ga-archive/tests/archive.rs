use std::{
    fs::{self, Permissions},
    io::{self, Read},
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::Command,
    sync::{Barrier, mpsc},
    time::Duration,
};

use ga_archive::{Archive, ArchiveError, ArchiveKey, ArchiveKind, PutLimits};

const INPUT: &[u8] = include_bytes!("../../ga-domain/tests/fixtures/legacy-json.input");
const PYTHON_ARCHIVE: &[u8] = include_bytes!("../../ga-domain/tests/fixtures/legacy-json.archive");
const PYTHON_KEY: &str = "24/24d8dd0ad99ca1b68513e606fbb9143ca14bffee06bb0dc9dc74d221a0cf54f5.json";
const SMALL: PutLimits = PutLimits {
    max_bytes: 4096,
    disk_reserve_bytes: 0,
};

fn directory() -> io::Result<(tempfile::TempDir, PathBuf)> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().canonicalize()?.join("raw");
    Ok((temp, path))
}

fn permission_bits(path: &Path) -> io::Result<u32> {
    Ok(fs::metadata(path)?.permissions().mode() & 0o777)
}

#[test]
fn reads_and_reuses_an_existing_python_archive_key() -> Result<(), Box<dyn std::error::Error>> {
    let (_temp, root) = directory()?;
    let reference = root.join(PYTHON_KEY);
    fs::create_dir_all(root.join("24"))?;
    fs::write(&reference, PYTHON_ARCHIVE)?;
    fs::set_permissions(&reference, Permissions::from_mode(0o600))?;

    let archive = Archive::open(&root)?;
    let key = PYTHON_KEY.parse::<ArchiveKey>()?;
    let mut bytes = Vec::new();
    archive.open_reader(&key)?.read_to_end(&mut bytes)?;
    assert_eq!(bytes, PYTHON_ARCHIVE);
    assert_eq!(archive.put_json(INPUT, SMALL)?, key);
    assert_eq!(permission_bits(&root)?, 0o700);
    assert_eq!(permission_bits(&root.join("24"))?, 0o700);
    assert_eq!(permission_bits(&reference)?, 0o600);
    assert_eq!(fs::read_dir(root)?.count(), 1);
    Ok(())
}

#[test]
fn rejects_paths_other_than_legacy_content_addressed_keys() {
    for key in [
        format!("/{PYTHON_KEY}"),
        format!("../{PYTHON_KEY}"),
        PYTHON_KEY.replace("24/", "25/"),
        PYTHON_KEY.to_uppercase(),
        PYTHON_KEY.replace(".json", ".txt"),
        "24/nested/name.json".to_owned(),
        "inline:0123456789".to_owned(),
    ] {
        assert!(key.parse::<ArchiveKey>().is_err());
    }
}

#[test]
fn streaming_writes_100_mib_without_an_input_buffer() -> Result<(), Box<dyn std::error::Error>> {
    const SIZE: u64 = 100 * 1024 * 1024;
    let (_temp, root) = directory()?;
    let archive = Archive::open(&root)?;
    let key = archive.put_reader(
        io::repeat(0xa7).take(SIZE),
        ArchiveKind::Fit,
        PutLimits {
            max_bytes: SIZE,
            disk_reserve_bytes: 0,
        },
    )?;
    assert_eq!(
        key.to_string(),
        "22/2279306afc5fd86754d2e0f92e8af4adecc15bc1aea93c17945569a973a502e9.fit"
    );
    let mut file = archive.open_reader(&key)?;
    assert_eq!(file.metadata()?.len(), SIZE);
    assert_eq!(io::copy(&mut file, &mut io::sink())?, SIZE);
    assert_eq!(permission_bits(&root.join(key.to_string()))?, 0o600);
    Ok(())
}

#[test]
fn simultaneous_writers_publish_one_complete_object() -> Result<(), Box<dyn std::error::Error>> {
    let (_temp, root) = directory()?;
    let archive = Archive::open(&root)?;
    let ready = Barrier::new(8);
    let keys = std::thread::scope(|scope| {
        let workers: Vec<_> = (0..8)
            .map(|_| {
                scope.spawn(|| {
                    ready.wait();
                    archive.put_reader(b"synthetic file".as_slice(), ArchiveKind::Binary, SMALL)
                })
            })
            .collect();
        workers
            .into_iter()
            .map(|worker| {
                worker
                    .join()
                    .map_err(|_| io::Error::other("Archive worker panicked"))?
            })
            .collect::<Result<Vec<_>, ArchiveError>>()
    })?;
    for key in &keys {
        assert_eq!(key, &keys[0]);
    }
    assert_eq!(fs::read_dir(&root)?.count(), 1);
    assert_eq!(fs::read(root.join(keys[0].to_string()))?, b"synthetic file");
    Ok(())
}

#[test]
fn rejects_symlink_roots_prefixes_and_objects() -> Result<(), Box<dyn std::error::Error>> {
    let (temp, root) = directory()?;
    let outside = temp.path().canonicalize()?.join("elsewhere");
    fs::create_dir(&outside)?;
    symlink(&outside, &root)?;
    assert!(Archive::open(&root).is_err());
    assert!(Archive::open(&root.join("ancestor")).is_err());
    fs::remove_file(&root)?;

    let archive = Archive::open(&root)?;
    let key = PYTHON_KEY.parse::<ArchiveKey>()?;
    let prefix = root.join("24");
    symlink(&outside, &prefix)?;
    assert!(archive.open_reader(&key).is_err());
    assert!(archive.put_json(INPUT, SMALL).is_err());
    assert_eq!(fs::read_dir(&outside)?.count(), 0);
    fs::remove_file(&prefix)?;
    fs::create_dir(&prefix)?;

    let protected = outside.join("keep");
    fs::write(&protected, b"protected")?;
    symlink(&protected, root.join(PYTHON_KEY))?;
    assert!(matches!(
        archive.open_reader(&key),
        Err(ArchiveError::InvalidObject)
    ));
    assert!(matches!(
        archive.put_json(INPUT, SMALL),
        Err(ArchiveError::InvalidObject)
    ));
    assert_eq!(fs::read(&protected)?, b"protected");
    assert!(fs::symlink_metadata(root.join(PYTHON_KEY))?.is_symlink());
    Ok(())
}

#[test]
fn corrupt_collisions_keep_the_existing_file() -> Result<(), Box<dyn std::error::Error>> {
    let (_temp, root) = directory()?;
    let archive = Archive::open(&root)?;
    let path = root.join(archive.put_json(INPUT, SMALL)?.to_string());
    let corrupt = vec![b'x'; PYTHON_ARCHIVE.len()];
    fs::write(&path, &corrupt)?;
    assert!(matches!(
        archive.put_json(INPUT, SMALL),
        Err(ArchiveError::ExistingObjectDigestMismatch)
    ));
    assert_eq!(fs::read(&path)?, corrupt);
    Ok(())
}

#[test]
fn too_large_or_near_full_put_never_publishes() -> Result<(), Box<dyn std::error::Error>> {
    let (_temp, root) = directory()?;
    let archive = Archive::open(&root)?;
    assert!(matches!(
        archive.put_reader(
            io::repeat(b'X').take(65537),
            ArchiveKind::Zip,
            PutLimits {
                max_bytes: 65536,
                disk_reserve_bytes: 0
            },
        ),
        Err(ArchiveError::PayloadTooLarge)
    ));
    assert!(matches!(
        archive.put_reader(
            b"one byte too many".as_slice(),
            ArchiveKind::Zip,
            PutLimits {
                max_bytes: 4096,
                disk_reserve_bytes: u64::MAX
            },
        ),
        Err(ArchiveError::InsufficientSpace)
    ));
    assert_eq!(fs::read_dir(&root)?.count(), 0);
    Ok(())
}

struct ExitAfterFirstChunk(bool);

impl Read for ExitAfterFirstChunk {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if self.0 {
            std::process::exit(79);
        }
        self.0 = true;
        let fragment = b"unterminated synthetic transfer";
        output[..fragment.len()].copy_from_slice(fragment);
        Ok(fragment.len())
    }
}

#[test]
fn archive_crash_child() -> Result<(), Box<dyn std::error::Error>> {
    if let Some(root) = std::env::var_os("GA_TEST_ARCHIVE_CHILD_DIR") {
        let archive = Archive::open(Path::new(&root))?;
        archive.put_reader(ExitAfterFirstChunk(false), ArchiveKind::Fit, SMALL)?;
        return Err("Expected the child to exit during writing".into());
    }
    Ok(())
}

#[test]
fn process_exit_mid_write_leaves_only_unpublished_staging() -> Result<(), Box<dyn std::error::Error>>
{
    let (_temp, root) = directory()?;
    let status = Command::new(std::env::current_exe()?)
        .args(["--exact", "archive_crash_child"])
        .env("GA_TEST_ARCHIVE_CHILD_DIR", &root)
        .output()?
        .status;
    assert_eq!(status.code(), Some(79));
    let entries = fs::read_dir(&root)?.collect::<Result<Vec<_>, _>>()?;
    assert_eq!(entries.len(), 1);
    assert!(
        entries[0]
            .file_name()
            .to_string_lossy()
            .starts_with(".rust-stage-")
    );

    let archive = Archive::open(&root)?;
    let lookalike = root.join(".rust-stage-00000000000000000000000000000001");
    let unrelated = root.join(".rust-stage-backup");
    fs::write(&unrelated, b"retain")?;
    symlink(&unrelated, &lookalike)?;
    assert_eq!(archive.remove_orphaned_staging()?, 1);
    assert_eq!(archive.remove_orphaned_staging()?, 0);
    assert!(fs::symlink_metadata(lookalike)?.is_symlink());
    assert_eq!(fs::read(unrelated)?, b"retain");

    let key = archive.put_reader(
        b"unterminated synthetic transfer".as_slice(),
        ArchiveKind::Fit,
        SMALL,
    )?;
    assert_eq!(
        fs::read(root.join(key.to_string()))?,
        b"unterminated synthetic transfer"
    );
    Ok(())
}

#[test]
fn cannot_sweep_active_writes() -> Result<(), Box<dyn std::error::Error>> {
    let (_temp, root) = directory()?;
    let writer_archive = Archive::open(&root)?;
    let maintenance_archive = Archive::open(&root)?;
    struct DelayedInput {
        writing: mpsc::Sender<()>,
        resume: mpsc::Receiver<()>,
        done: bool,
    }
    impl Read for DelayedInput {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            if self.done {
                return Ok(0);
            }
            self.writing
                .send(())
                .map_err(|_| io::Error::other("Archive test ended during a write"))?;
            self.resume
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| io::Error::other("Archive test did not resume the write"))?;
            self.done = true;
            output[0] = b'a';
            Ok(1)
        }
    }

    let (attempt, key) = std::thread::scope(|scope| {
        let (writing, started) = mpsc::channel();
        let (resume, resumed) = mpsc::channel();
        let writer = scope.spawn(|| {
            writer_archive.put_reader(
                DelayedInput {
                    writing,
                    resume: resumed,
                    done: false,
                },
                ArchiveKind::Binary,
                SMALL,
            )
        });
        started
            .recv_timeout(Duration::from_secs(5))
            .map_err(|_| io::Error::other("Archive writer did not start"))?;
        let attempt = maintenance_archive.remove_orphaned_staging();
        let _ = resume.send(());
        let key = writer
            .join()
            .map_err(|_| io::Error::other("Archive writer panicked"))??;
        Ok::<_, ArchiveError>((attempt, key))
    })?;
    assert!(matches!(attempt, Err(ArchiveError::StagingBusy)));
    assert_eq!(maintenance_archive.remove_orphaned_staging()?, 0);
    assert_eq!(fs::read(root.join(key.to_string()))?, b"a");
    Ok(())
}

#[test]
#[ignore = "requires a dedicated small writable filesystem"]
fn disk_full_aborts_and_preserves_existing_archive() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(std::env::var_os("GA_TEST_ARCHIVE_FULL_DIR").ok_or_else(|| {
        io::Error::other("Set GA_TEST_ARCHIVE_FULL_DIR to an empty 8 MiB filesystem")
    })?);
    assert_eq!(fs::read_dir(&root)?.count(), 0);
    let archive = Archive::open(&root)?;
    let original = archive.put_json(INPUT, SMALL)?;
    let result = archive.put_reader(
        io::repeat(b'Z').take(12 * 1024 * 1024),
        ArchiveKind::Zip,
        PutLimits {
            max_bytes: 12 * 1024 * 1024,
            disk_reserve_bytes: 0,
        },
    );
    assert!(matches!(result, Err(ArchiveError::InsufficientSpace)));
    assert_eq!(fs::read(root.join(original.to_string()))?, PYTHON_ARCHIVE);
    assert_eq!(fs::read_dir(&root)?.count(), 1);
    let next = archive.put_reader(b"retry".as_slice(), ArchiveKind::Zip, SMALL)?;
    assert_eq!(fs::read(root.join(next.to_string()))?, b"retry");
    Ok(())
}
