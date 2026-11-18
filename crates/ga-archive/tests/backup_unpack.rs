use std::{env, error::Error, fs, io::Write, os::unix::fs::PermissionsExt, path::PathBuf};

use flate2::{Compression, write::GzEncoder};
use ga_archive::{
    backup_envelope::{BackupError, write_encrypted_backup},
    backup_unpack::unpack_backup,
};
use tar::{Builder, EntryType, Header};

const FIXTURE_BYTES: [u8; 32] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28, 29, 30, 31,
];

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
