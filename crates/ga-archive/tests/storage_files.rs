use std::{
    env,
    error::Error,
    fs,
    io::{Read, Write},
    os::unix::fs::symlink,
    path::PathBuf,
};

use ga_archive::storage_files::{StorageAccess, StorageFilesError, StorageFilesGuard};

#[test]
fn blocks_redirects_and_keeps_erased_storage_inactive() -> Result<(), Box<dyn Error>> {
    let root = tempfile::tempdir()?;
    let directory = root.path().canonicalize()?.join("locks");
    let active = StorageFilesGuard::acquire(&directory, StorageAccess::Active)?;
    assert!(matches!(
        StorageFilesGuard::acquire(&directory, StorageAccess::Recovery),
        Err(StorageFilesError::Occupied),
    ));
    drop(active);

    for marker in ["erased", "activating"] {
        let marker = directory.join(marker);
        fs::write(&marker, b"synthetic marker")?;
        assert!(matches!(
            StorageFilesGuard::acquire(&directory, StorageAccess::Active),
            Err(StorageFilesError::Disabled),
        ));
        drop(StorageFilesGuard::acquire(
            &directory,
            StorageAccess::Recovery,
        )?);
        assert_eq!(fs::read(&marker)?, b"synthetic marker");
        fs::remove_file(marker)?;
    }
    let redirect = directory.with_file_name("redirect");
    symlink(&directory, &redirect)?;
    assert!(matches!(
        StorageFilesGuard::acquire(&redirect.join("locks"), StorageAccess::Active),
        Err(StorageFilesError::Unsafe),
    ));
    let file = directory.join("storage.lock");
    fs::remove_file(&file)?;
    let unrelated = directory.join("unrelated");
    fs::write(&unrelated, b"leave intact")?;
    symlink(&unrelated, file)?;
    assert!(matches!(
        StorageFilesGuard::acquire(&directory, StorageAccess::Active),
        Err(StorageFilesError::Unsafe),
    ));
    assert_eq!(fs::read(unrelated)?, b"leave intact");
    Ok(())
}

#[test]
#[ignore = "requires the independent Python maintenance-lock fixture"]
fn shares_one_lock_with_python() -> Result<(), Box<dyn Error>> {
    let directory =
        PathBuf::from(env::var_os("GA_TEST_STORAGE_FILES").ok_or("Missing test directory")?);
    match env::var("GA_TEST_STORAGE_MODE")?.as_str() {
        "blocked" => {
            assert!(matches!(
                StorageFilesGuard::acquire(&directory, StorageAccess::Active),
                Err(StorageFilesError::Occupied),
            ));
        }
        "hold" => {
            let _guard = StorageFilesGuard::acquire(&directory, StorageAccess::Active)?;
            let mut stdout = std::io::stdout().lock();
            writeln!(stdout, "NATIVE_LOCK_ACQUIRED")?;
            stdout.flush()?;
            let mut end = [0];
            std::io::stdin().read_exact(&mut end)?;
        }
        _ => return Err("Unknown test mode".into()),
    }
    Ok(())
}
