use std::{
    env,
    error::Error,
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
};

use ga_archive::{
    ArchiveKey,
    backup_bundle::{BackupBundle, write_backup_bundle},
    backup_envelope::verify_encrypted_backup,
};
use sha2::{Digest, Sha256};

const FIXTURE_BYTES: [u8; 32] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28, 29, 30, 31,
];

fn bundle<'a>(
    data_dir: &'a Path,
    token_dir: &'a Path,
    database_export: &'a Path,
    required_raw: &'a [ArchiveKey],
) -> BackupBundle<'a> {
    BackupBundle {
        data_dir,
        token_dir,
        database_export,
        required_raw,
    }
}

#[test]
#[ignore = "requires a new cross-language synthetic fixture"]
fn captures_the_existing_legacy_bundle_inventory() -> Result<(), Box<dyn Error>> {
    let root =
        PathBuf::from(env::var_os("GA_TEST_BACKUP_BUNDLE").ok_or("Missing synthetic fixture")?);
    let required = fs::read_to_string(root.join("required.txt"))?.parse::<ArchiveKey>()?;
    let result = write_backup_bundle(
        &root.join("rust.backup"),
        &FIXTURE_BYTES,
        bundle(
            &root.join("data"),
            &root.join("secrets"),
            &root.join("database.jsonl.gz"),
            &[required],
        ),
    )?;
    assert_eq!(
        result,
        verify_encrypted_backup(&root.join("rust.backup"), &FIXTURE_BYTES)?
    );
    assert!(result.bytes > 48 * 1024 * 1024);
    println!("Verified compressed backup bytes: {}", result.bytes);
    Ok(())
}

#[test]
fn refuses_missing_changed_or_redirected_sources_without_replacing_prior_bundle()
-> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let data = root.join("data");
    let tokens = root.join("tokens");
    let snapshot = root.join("database.jsonl.gz");
    fs::create_dir_all(&tokens)?;
    fs::create_dir_all(data.join("raw"))?;
    fs::write(&snapshot, b"synthetic staged database")?;
    let contents = b"required synthetic raw";
    let digest = hex::encode(Sha256::digest(contents));
    let key = format!("{}/{}.bin", &digest[..2], digest);
    let required = [key.parse::<ArchiveKey>()?];
    let raw = data.join("raw").join(key);
    fs::create_dir_all(raw.parent().ok_or("Missing raw path parent")?)?;
    let existing = root.join("existing.backup");
    let previous = write_backup_bundle(
        &existing,
        &FIXTURE_BYTES,
        bundle(&data, &tokens, &snapshot, &[]),
    )?;
    let output = root.join("failed.backup");
    assert!(
        write_backup_bundle(
            &output,
            &FIXTURE_BYTES,
            bundle(&data, &tokens, &snapshot, &required),
        )
        .is_err()
    );
    assert!(!output.exists());
    fs::write(&raw, b"wrong synthetic raw")?;
    assert!(
        write_backup_bundle(
            &output,
            &FIXTURE_BYTES,
            bundle(&data, &tokens, &snapshot, &required),
        )
        .is_err()
    );
    assert!(!output.exists());
    fs::write(&raw, contents)?;
    symlink(&snapshot, tokens.join("redirect"))?;
    assert!(
        write_backup_bundle(
            &output,
            &FIXTURE_BYTES,
            bundle(&data, &tokens, &snapshot, &required),
        )
        .is_err()
    );
    assert!(!output.exists());
    fs::remove_file(tokens.join("redirect"))?;
    assert!(
        write_backup_bundle(
            &data.join("unsafe.backup"),
            &FIXTURE_BYTES,
            bundle(&data, &tokens, &snapshot, &required),
        )
        .is_err()
    );
    assert!(!data.join("unsafe.backup").exists());
    write_backup_bundle(
        &output,
        &FIXTURE_BYTES,
        bundle(&data, &tokens, &snapshot, &required),
    )?;
    assert_eq!(
        verify_encrypted_backup(&existing, &FIXTURE_BYTES)?,
        previous
    );
    Ok(())
}
