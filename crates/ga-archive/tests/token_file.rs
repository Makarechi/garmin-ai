use std::{
    error::Error,
    fs,
    os::unix::fs::{PermissionsExt, symlink},
};

use ga_archive::{ArchiveError, GarminTokenFile};

#[test]
fn private_token_files_can_be_rotated_and_reopened() -> Result<(), Box<dyn Error>> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?.join("tokens");
    let store = GarminTokenFile::open(&root)?;
    let guard = store.try_lock()?;
    assert!(guard.read()?.is_none());
    assert!(matches!(store.try_lock(), Err(ArchiveError::StagingBusy)));
    guard.replace(b"synthetic initial session")?;
    assert_eq!(
        guard.read()?.as_deref(),
        Some(b"synthetic initial session".as_slice())
    );
    assert_eq!(fs::metadata(&root)?.permissions().mode() & 0o777, 0o700);
    let path = root.join("garmin_tokens.json");
    assert_eq!(fs::metadata(&path)?.permissions().mode() & 0o777, 0o600);
    assert!(matches!(
        guard.replace(&vec![0; 65_537]),
        Err(ArchiveError::PayloadTooLarge)
    ));
    assert_eq!(fs::read(&path)?, b"synthetic initial session");
    guard.replace(b"synthetic rotated session")?;
    drop(guard);
    drop(store);
    let restored = GarminTokenFile::open(&root)?;
    assert_eq!(
        restored.try_lock()?.read()?.as_deref(),
        Some(b"synthetic rotated session".as_slice())
    );
    assert_eq!(fs::read_dir(&root)?.count(), 1);
    Ok(())
}

#[test]
fn rejects_aliases_unsafe_permissions_and_oversized_existing_files() -> Result<(), Box<dyn Error>> {
    let temporary = tempfile::tempdir()?;
    let work = temporary.path().canonicalize()?;
    let root = work.join("tokens");
    let target = work.join("safe.txt");
    fs::write(&target, b"synthetic untouched")?;
    fs::set_permissions(&target, fs::Permissions::from_mode(0o600))?;
    let store = GarminTokenFile::open(&root)?;
    let path = root.join("garmin_tokens.json");
    let guard = store.try_lock()?;
    symlink(&target, &path)?;
    assert!(matches!(guard.read(), Err(ArchiveError::InvalidObject)));
    assert!(matches!(
        guard.replace(b"synthetic changed"),
        Err(ArchiveError::InvalidObject)
    ));
    fs::remove_file(&path)?;
    fs::hard_link(&target, &path)?;
    assert!(matches!(guard.read(), Err(ArchiveError::InvalidObject)));
    fs::remove_file(&path)?;
    assert_eq!(fs::read(&target)?, b"synthetic untouched");
    fs::write(&path, vec![0; 65_537])?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644))?;
    assert!(matches!(guard.read(), Err(ArchiveError::InvalidObject)));
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    assert!(matches!(guard.read(), Err(ArchiveError::PayloadTooLarge)));
    let alias = work.join("alias");
    symlink(&root, &alias)?;
    assert!(GarminTokenFile::open(&alias).is_err());
    Ok(())
}
