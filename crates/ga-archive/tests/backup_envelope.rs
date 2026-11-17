use std::{
    env,
    error::Error,
    fs::{self, File},
    io::{self, Read, Seek, SeekFrom, Write},
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
};

use ga_archive::backup_envelope::{
    BackupError, decrypt_encrypted_backup, verify_encrypted_backup, write_encrypted_backup,
};
use sha2::{Digest, Sha256};

const FIXTURE_BYTES: [u8; 32] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28, 29, 30, 31,
];

#[test]
#[ignore = "uses synthetic cross-language backup files produced by the pinned Python fixture"]
fn streams_existing_python_format_both_directions() -> Result<(), Box<dyn Error>> {
    let root =
        PathBuf::from(env::var_os("GA_TEST_BACKUP_ENVELOPE").ok_or("Missing synthetic fixture")?);
    let reference = verify_encrypted_backup(&root.join("python.backup"), &FIXTURE_BYTES)?;
    assert!(reference.bytes > 64 * 1024 * 1024);
    let output = root.join("rust-decoded.bin");
    let decrypted = decrypt_encrypted_backup(&root.join("python.backup"), &output, &FIXTURE_BYTES)?;
    assert_eq!(reference, decrypted);
    assert_eq!(digest(&output)?, digest(&root.join("source.bin"))?);
    assert_eq!(fs::metadata(&output)?.permissions().mode() & 0o777, 0o600);
    let encrypted = root.join("rust.backup");
    let rust = write_encrypted_backup(&encrypted, &FIXTURE_BYTES, |writer| {
        io::copy(&mut File::open(root.join("source.bin"))?, writer)?;
        Ok(())
    })?;
    assert_eq!(reference, rust);
    assert_eq!(verify_encrypted_backup(&encrypted, &FIXTURE_BYTES)?, rust);
    assert_eq!(fs::metadata(&encrypted)?.len(), rust.bytes + 37);
    let empty = verify_encrypted_backup(&root.join("empty.backup"), &FIXTURE_BYTES)?;
    assert_eq!(empty.bytes, 0);
    assert_eq!(empty.sha256, Sha256::digest([]).as_slice());
    println!(
        "Authenticated {} synthetic bytes in both backup directions",
        rust.bytes
    );
    Ok(())
}

#[test]
fn refuses_damage_and_preserves_prior_backup_when_publish_fails() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let existing = root.join("confirmed.backup");
    let initial = write_encrypted_backup(&existing, &FIXTURE_BYTES, |writer| {
        writer.write_all(b"synthetic confirmed backup")
    })?;
    assert!(matches!(
        write_encrypted_backup(&existing, &FIXTURE_BYTES, |_| panic!(
            "existing destination was not rejected"
        )),
        Err(BackupError::Exists)
    ));
    let failure = root.join("interrupted.backup");
    assert!(
        write_encrypted_backup(&failure, &FIXTURE_BYTES, |writer| {
            writer.write_all(b"part of a synthetic backup")?;
            Err(io::ErrorKind::StorageFull.into())
        })
        .is_err()
    );
    assert!(!failure.exists());

    let competing = root.join("competing.backup");
    assert!(matches!(
        write_encrypted_backup(&competing, &FIXTURE_BYTES, |writer| {
            writer.write_all(b"stream bytes")?;
            fs::write(&competing, b"already published")
        }),
        Err(BackupError::Exists)
    ));
    assert_eq!(fs::read(competing)?, b"already published");

    let wrong = root.join("wrong-key.bin");
    assert!(matches!(
        decrypt_encrypted_backup(&existing, &wrong, &[255; 32]),
        Err(BackupError::Authentication)
    ));
    assert!(!wrong.exists());
    let damaged = root.join("damaged.backup");
    fs::copy(&existing, &damaged)?;
    let mut file = File::options().read(true).write(true).open(&damaged)?;
    file.seek(SeekFrom::End(-1))?;
    let mut byte = [0; 1];
    file.read_exact(&mut byte)?;
    file.seek(SeekFrom::End(-1))?;
    file.write_all(&[byte[0] ^ 1])?;
    assert!(matches!(
        decrypt_encrypted_backup(&damaged, &wrong, &FIXTURE_BYTES),
        Err(BackupError::Authentication)
    ));
    assert!(!wrong.exists());
    fs::write(&damaged, b"short")?;
    assert!(matches!(
        verify_encrypted_backup(&damaged, &FIXTURE_BYTES),
        Err(BackupError::InvalidFrame)
    ));

    let redirect = root.join("redirect");
    symlink(&root, &redirect)?;
    assert!(
        write_encrypted_backup(&redirect.join("forbidden.backup"), &FIXTURE_BYTES, |_| Ok(
            ()
        ))
        .is_err()
    );
    assert!(!root.join("forbidden.backup").exists());
    assert_eq!(verify_encrypted_backup(&existing, &FIXTURE_BYTES)?, initial);
    for entry in fs::read_dir(root)? {
        assert!(
            !entry?
                .file_name()
                .to_string_lossy()
                .starts_with(".rust-backup-")
        );
    }
    Ok(())
}

fn digest(path: &Path) -> io::Result<[u8; 32]> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0; 64 * 1024];
    loop {
        let bytes = file.read(&mut buffer)?;
        if bytes == 0 {
            break;
        }
        digest.update(&buffer[..bytes]);
    }
    Ok(digest.finalize().into())
}
