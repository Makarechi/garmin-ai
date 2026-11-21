use std::{
    env,
    error::Error,
    fs,
    io::Write,
    os::unix::fs::{PermissionsExt, symlink},
    path::PathBuf,
};

use flate2::{Compression, write::GzEncoder};
use ga_archive::{
    backup_bundle::{BackupBundle, write_backup_bundle},
    backup_envelope::{BackupError, write_encrypted_backup},
    backup_unpack::unpack_backup,
};
use tar::{Builder, EntryType, Header};

const FIXTURE_BYTES: [u8; 32] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28, 29, 30, 31,
];

#[test]
fn restores_a_bundle_with_the_deepest_supported_tree() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let data = root.join("data");
    let tokens = root.join("tokens");
    let export = root.join("export");
    let mut name = PathBuf::from("raw");
    name.extend(std::iter::repeat_n("d", 128));
    let leaf = data.join(&name);
    fs::create_dir_all(&leaf)?;
    fs::write(leaf.join("record"), b"deep synthetic record")?;
    fs::write(&export, b"synthetic export")?;
    let source = root.join("backup");
    write_backup_bundle(
        &source,
        &FIXTURE_BYTES,
        BackupBundle {
            data_dir: &data,
            token_dir: &tokens,
            database_export: &export,
            required_raw: &[],
        },
    )?;
    let recovery = root.join("recovered");
    unpack_backup(&source, &recovery, &FIXTURE_BYTES, &data, &tokens)?;
    assert_eq!(
        fs::read(recovery.join(name).join("record"))?,
        b"deep synthetic record"
    );
    Ok(())
}

#[test]
fn protects_resolved_data_and_token_directories() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let live = root.join("live");
    fs::create_dir_all(live.join("present"))?;
    let alias = root.join("alias");
    symlink(live.join("present"), &alias)?;
    let source = root.join("unused");
    let independent = root.join("independent");
    for (target, data, tokens) in [
        (live.join("recovery"), alias.join(".."), independent.clone()),
        (
            live.join("present/recovery"),
            independent.clone(),
            alias.clone(),
        ),
        (
            live.join("recovery"),
            independent,
            alias.join("../recovery/tokens"),
        ),
    ] {
        assert!(matches!(
            unpack_backup(&source, &target, &FIXTURE_BYTES, &data, &tokens),
            Err(BackupError::InvalidPath)
        ));
        assert!(!target.exists());
    }
    Ok(())
}

#[test]
#[ignore = "requires independently generated Python PAX fixture"]
fn restores_python_compressed_and_uncompressed_backups() -> Result<(), Box<dyn Error>> {
    let root =
        PathBuf::from(env::var_os("GA_TEST_BACKUP_UNPACK").ok_or("Missing synthetic fixture")?);
    for variant in ["compressed", "uncompressed"] {
        let result = unpack_backup(
            &root.join(format!("{variant}.backup")),
            &root.join(format!("rust-{variant}")),
            &FIXTURE_BYTES,
            &root.join("protected-data"),
            &root.join("protected-tokens"),
        )?;
        assert!(result.bytes > 48 * 1024 * 1024);
        println!(
            "Restored {variant} Python package, {} authenticated bytes",
            result.bytes
        );
    }
    Ok(())
}

#[test]
fn invalid_archives_leave_no_recovered_files_or_staging() -> Result<(), Box<dyn Error>> {
    let tmp = tempfile::tempdir()?;
    let root = tmp.path().canonicalize()?;
    let output = root.join("recovered");
    let data = root.join("protected-data");
    let tokens = root.join("protected-tokens");
    let source = root.join("bad.backup");
    for (name, kind, content) in [
        ("../outside", EntryType::Regular, b"unsafe".as_slice()),
        ("raw/../outside", EntryType::Regular, b"unsafe".as_slice()),
        ("/absolute", EntryType::Regular, b"unsafe".as_slice()),
        ("raw/link", EntryType::Symlink, b"".as_slice()),
        ("unexpected.txt", EntryType::Regular, b"unsafe".as_slice()),
        (
            "database.jsonl.gz",
            EntryType::Regular,
            b"duplicate".as_slice(),
        ),
    ] {
        let mut tar = Builder::new(Vec::new());
        append(
            &mut tar,
            "database.jsonl.gz",
            EntryType::Regular,
            b"synthetic staged export",
        )?;
        append(&mut tar, name, kind, content)?;
        let bytes = tar.into_inner()?;
        write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| writer.write_all(&bytes))?;
        assert!(
            unpack_backup(&source, &output, &FIXTURE_BYTES, &data, &tokens).is_err(),
            "{name}"
        );
        assert!(!output.exists());
        fs::remove_file(&source)?;
    }
    let mut tar = Builder::new(Vec::new());
    append(
        &mut tar,
        "database.jsonl.gz",
        EntryType::Regular,
        b"synthetic staged export",
    )?;
    let good = tar.into_inner()?;
    let mut bad = good.clone();
    bad[3] ^= 1;
    write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| writer.write_all(&bad))?;
    assert!(unpack_backup(&source, &output, &FIXTURE_BYTES, &data, &tokens).is_err());
    assert!(!output.exists());
    fs::remove_file(&source)?;
    let mut gzip = GzEncoder::new(Vec::new(), Compression::fast());
    gzip.write_all(&good)?;
    let mut damaged_gzip = gzip.finish()?;
    let checksum = damaged_gzip.len() - 8;
    damaged_gzip[checksum] ^= 1;
    write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| {
        writer.write_all(&damaged_gzip)
    })?;
    assert!(unpack_backup(&source, &output, &FIXTURE_BYTES, &data, &tokens).is_err());
    assert!(!output.exists());
    fs::remove_file(&source)?;
    write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| writer.write_all(&good))?;
    assert!(matches!(
        unpack_backup(&source, &output, &[0; 32], &data, &tokens),
        Err(BackupError::Authentication)
    ));
    assert!(!output.exists());
    fs::create_dir(&output)?;
    fs::write(output.join("existing"), b"last valid recovery")?;
    assert!(matches!(
        unpack_backup(&source, &output, &FIXTURE_BYTES, &data, &tokens),
        Err(BackupError::Exists)
    ));
    assert_eq!(fs::read(output.join("existing"))?, b"last valid recovery");
    assert!(matches!(
        unpack_backup(
            &source,
            &data.join("recovery"),
            &FIXTURE_BYTES,
            &data,
            &tokens
        ),
        Err(BackupError::InvalidPath)
    ));
    for entry in fs::read_dir(&root)? {
        assert!(
            !entry?
                .file_name()
                .to_string_lossy()
                .starts_with(".rust-backup-")
        );
    }
    let success = root.join("independent");
    unpack_backup(&source, &success, &FIXTURE_BYTES, &data, &tokens)?;
    let recovered = success.join("database.jsonl.gz");
    assert_eq!(
        fs::metadata(&recovered)?.permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(fs::read(recovered)?, b"synthetic staged export");
    Ok(())
}

fn append(
    archive: &mut Builder<impl Write>,
    name: &str,
    kind: EntryType,
    bytes: &[u8],
) -> std::io::Result<()> {
    let mut header = Header::new_gnu();
    header.set_entry_type(kind);
    header.set_size(bytes.len() as u64);
    header.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
    header.set_cksum();
    archive.append(&header, bytes)
}

#[test]
fn retry_clears_abandoned_plaintext_before_authentication() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let workspace = root.join(".garmin-ai-plaintext");
    let active = workspace.join("active");
    fs::create_dir_all(active.join("contents/raw"))?;
    fs::set_permissions(&workspace, fs::Permissions::from_mode(0o700))?;
    fs::write(workspace.join("operation.lock"), b"")?;
    fs::write(active.join("backup.tar"), b"interrupted plaintext")?;
    fs::write(active.join("contents/raw/partial"), b"partially restored")?;
    let retained = root.join("previous-recovery");
    fs::write(&retained, b"keep verified recovery")?;

    let source = root.join("backup");
    write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| {
        let mut archive = Builder::new(writer);
        append(
            &mut archive,
            "database.jsonl.gz",
            EntryType::Regular,
            b"verified synthetic export",
        )?;
        archive.finish()
    })?;
    let destination = root.join("restored");
    let data = root.join("data");
    let tokens = root.join("tokens");
    assert!(matches!(
        unpack_backup(&source, &destination, &[0; 32], &data, &tokens),
        Err(BackupError::Authentication)
    ));
    assert!(!active.exists(), "interrupted plaintext survived the retry");
    assert!(!destination.exists());
    unpack_backup(&source, &destination, &FIXTURE_BYTES, &data, &tokens)?;
    assert_eq!(
        fs::read(destination.join("database.jsonl.gz"))?,
        b"verified synthetic export"
    );
    assert_eq!(fs::read(retained)?, b"keep verified recovery");
    assert!(!active.exists());
    Ok(())
}

#[test]
fn does_not_reap_an_active_recovery() -> Result<(), Box<dyn Error>> {
    use rustix::fs::FlockOperation;

    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let workspace = root.join(".garmin-ai-plaintext");
    let active = workspace.join("active");
    fs::create_dir_all(&active)?;
    fs::set_permissions(&workspace, fs::Permissions::from_mode(0o700))?;
    let marker = active.join("backup.tar");
    fs::write(&marker, b"another process is restoring")?;
    let operation = fs::File::create(workspace.join("operation.lock"))?;
    rustix::fs::flock(&operation, FlockOperation::NonBlockingLockExclusive)?;

    let source = root.join("backup");
    write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| {
        let mut archive = Builder::new(writer);
        append(
            &mut archive,
            "database.jsonl.gz",
            EntryType::Regular,
            b"verified synthetic export",
        )?;
        archive.finish()
    })?;
    let destination = root.join("restored");
    let data = root.join("data");
    let tokens = root.join("tokens");
    assert!(unpack_backup(&source, &destination, &FIXTURE_BYTES, &data, &tokens).is_err());
    assert_eq!(fs::read(marker)?, b"another process is restoring");
    assert!(!destination.exists());

    drop(operation);
    unpack_backup(&source, &destination, &FIXTURE_BYTES, &data, &tokens)?;
    assert!(destination.join("database.jsonl.gz").is_file());
    assert!(!active.exists());
    Ok(())
}

#[test]
fn recovery_workspace_never_contains_the_input_output_or_live_storage() -> Result<(), Box<dyn Error>>
{
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let active = root.join(".garmin-ai-plaintext/active");
    fs::create_dir_all(&active)?;
    let nested = active.join("keep");
    fs::write(&nested, b"private data")?;
    let source = root.join("archive");
    fs::write(&source, b"placeholder ciphertext")?;
    let destination = root.join("recovery");
    let data = root.join("data");
    let tokens = root.join("tokens");

    for (input, target, live_data) in [
        (nested.as_path(), destination.as_path(), data.as_path()),
        (source.as_path(), active.as_path(), data.as_path()),
        (source.as_path(), destination.as_path(), active.as_path()),
    ] {
        assert!(matches!(
            unpack_backup(input, target, &FIXTURE_BYTES, live_data, &tokens),
            Err(BackupError::InvalidPath)
        ));
        assert_eq!(fs::read(&nested)?, b"private data");
        assert!(!destination.exists());
    }

    // An existing active directory without the operation lock is not managed staging.
    let workspace = active.parent().ok_or("missing workspace")?;
    fs::set_permissions(workspace, fs::Permissions::from_mode(0o700))?;
    assert!(matches!(
        unpack_backup(&source, &destination, &FIXTURE_BYTES, &data, &tokens),
        Err(BackupError::Workspace(
            ga_archive::storage_files::StorageFilesError::Unsafe
        ))
    ));
    assert_eq!(fs::read(nested)?, b"private data");
    assert!(!workspace.join("operation.lock").exists());
    Ok(())
}

#[test]
fn does_not_reap_an_aliased_backup_or_live_directory() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let parent = root.join("Recovery");
    fs::create_dir(&parent)?;
    let alias = root.join("RECOVERY");
    if !alias.exists() {
        // Exercise the actual lookup behavior when the fixture filesystem folds case.
        return Ok(());
    }

    let workspace = parent.join(".garmin-ai-plaintext");
    let active = workspace.join("active");
    let aliased_active = alias.join(".GARMIN-AI-PLAINTEXT/ACTIVE");
    fs::create_dir_all(&active)?;
    fs::set_permissions(&workspace, fs::Permissions::from_mode(0o700))?;
    fs::write(workspace.join("operation.lock"), b"")?;
    let original = active.join("archive");
    fs::write(&original, b"preserve the selected backup")?;
    let standalone = root.join("archive");
    fs::write(&standalone, b"another backup")?;
    let destination = parent.join("restored");
    let data = root.join("data");
    let tokens = root.join("tokens");

    for (input, data, tokens) in [
        (aliased_active.join("ARCHIVE"), data.clone(), tokens.clone()),
        (standalone.clone(), aliased_active.clone(), tokens),
        (standalone, data, aliased_active.join("future-tokens")),
    ] {
        let result = unpack_backup(&input, &destination, &FIXTURE_BYTES, &data, &tokens);
        assert_eq!(fs::read(&original)?, b"preserve the selected backup");
        assert!(
            matches!(result, Err(BackupError::InvalidPath)),
            "{result:?}"
        );
        assert!(!destination.exists());
    }
    assert!(matches!(
        unpack_backup(
            &root.join("archive"),
            &parent.join("inside-live-storage"),
            &FIXTURE_BYTES,
            &alias,
            &root.join("tokens"),
        ),
        Err(BackupError::InvalidPath)
    ));
    assert_eq!(fs::read(&original)?, b"preserve the selected backup");
    Ok(())
}

#[test]
fn rejects_a_redirected_source_before_reclaiming_staging() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let workspace = root.join(".garmin-ai-plaintext");
    let active = workspace.join("active");
    fs::create_dir_all(&active)?;
    fs::set_permissions(&workspace, fs::Permissions::from_mode(0o700))?;
    fs::write(workspace.join("operation.lock"), b"")?;
    let original = active.join("archive");
    fs::write(&original, b"preserve the selected backup")?;
    let source = root.join("redirected-backup");
    symlink(&original, &source)?;

    let destination = root.join("recovered");
    assert!(matches!(
        unpack_backup(
            &source,
            &destination,
            &FIXTURE_BYTES,
            &root.join("data"),
            &root.join("tokens")
        ),
        Err(BackupError::InvalidPath)
    ));
    assert_eq!(fs::read(original)?, b"preserve the selected backup");
    assert!(!destination.exists());
    Ok(())
}

#[test]
fn does_not_publish_into_a_reserved_workspace_name() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let source = root.join("backup");
    write_encrypted_backup(&source, &FIXTURE_BYTES, |writer| {
        let mut archive = Builder::new(writer);
        append(
            &mut archive,
            "database.jsonl.gz",
            EntryType::Regular,
            b"verified synthetic export",
        )?;
        archive.finish()
    })?;

    let destination = root.join(".GARMIN-AI-PLAINTEXT/active/recovered");
    assert!(matches!(
        unpack_backup(
            &source,
            &destination,
            &FIXTURE_BYTES,
            &root.join("data"),
            &root.join("tokens"),
        ),
        Err(BackupError::InvalidPath)
    ));
    assert!(!root.join(".GARMIN-AI-PLAINTEXT").exists());
    Ok(())
}
