use std::{error::Error, fs, os::unix::fs::symlink};

use ga_archive::erasure_files::{ErasureFiles, ErasureFilesError};

#[test]
fn removes_only_the_prepared_private_roots_and_can_retry() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?.join("fixture");
    let data = root.join("data");
    let tokens = root.join("tokens");
    let locks = root.join("locks");
    let backups = root.join("backups");
    for directory in [&data.join("raw/one/two"), &tokens, &locks, &backups] {
        fs::create_dir_all(directory)?;
    }
    for i in 0..500 {
        fs::write(data.join(format!("raw/one/two/{i}")), b"synthetic record")?;
    }
    fs::write(tokens.join("fixture"), b"synthetic integration file")?;
    fs::write(locks.join("erased"), b"maintenance marker")?;
    fs::write(backups.join("snapshot"), b"independent backup")?;

    ErasureFiles::prepare(&data, &tokens, &locks, &backups)?.remove()?;
    assert!(!data.exists() && !tokens.exists());
    assert_eq!(fs::read(locks.join("erased"))?, b"maintenance marker");
    assert_eq!(fs::read(backups.join("snapshot"))?, b"independent backup");
    ErasureFiles::prepare(&data, &tokens, &locks, &backups)?.remove()?;
    Ok(())
}

#[test]
fn refuses_overlaps_redirects_and_a_replaced_directory() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?.join("fixture");
    let data = root.join("data");
    let tokens = root.join("tokens");
    let locks = root.join("locks");
    let backups = root.join("backups");
    let separate = root.join("separate");
    for directory in [&data, &tokens, &separate] {
        fs::create_dir_all(directory)?;
    }
    fs::write(data.join("fixture"), b"private fixture")?;
    fs::write(separate.join("fixture"), b"unrelated fixture")?;
    for (data, tokens, locks, backups) in [
        (
            data.clone(),
            data.join("tokens"),
            locks.clone(),
            backups.clone(),
        ),
        (
            data.clone(),
            tokens.clone(),
            data.join("locks"),
            backups.clone(),
        ),
        (
            data.clone(),
            tokens.clone(),
            locks.clone(),
            data.join("backups"),
        ),
    ] {
        assert!(matches!(
            ErasureFiles::prepare(&data, &tokens, &locks, &backups),
            Err(ErasureFilesError::Overlap)
        ));
    }
    let alias = root.join("alias");
    symlink(&data, &alias)?;
    assert!(matches!(
        ErasureFiles::prepare(&alias, &tokens, &locks, &backups),
        Err(ErasureFilesError::UnsafePath)
    ));
    assert!(matches!(
        ErasureFiles::prepare(&data, &tokens, &locks, &alias.join("backup")),
        Err(ErasureFilesError::Overlap)
    ));
    let redirect = tokens.join("redirect");
    symlink(&separate, &redirect)?;
    assert!(matches!(
        ErasureFiles::prepare(&data, &tokens, &locks, &backups),
        Err(ErasureFilesError::UnsafePath)
    ));
    fs::remove_file(&redirect)?;
    assert_eq!(fs::read(data.join("fixture"))?, b"private fixture");

    let prepared = ErasureFiles::prepare(&data, &tokens, &locks, &backups)?;
    let moved = root.join("moved");
    fs::rename(&tokens, &moved)?;
    fs::create_dir(&tokens)?;
    fs::write(tokens.join("fixture"), b"replacement directory")?;
    assert!(matches!(
        prepared.remove(),
        Err(ErasureFilesError::UnsafePath)
    ));
    assert_eq!(fs::read(tokens.join("fixture"))?, b"replacement directory");
    assert_eq!(fs::read(separate.join("fixture"))?, b"unrelated fixture");
    assert_eq!(fs::read(data.join("fixture"))?, b"private fixture");
    Ok(())
}

#[test]
fn resumes_after_only_one_root_was_removed() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?.join("fixture");
    let data = root.join("data");
    let tokens = root.join("tokens");
    let locks = root.join("locks");
    let backups = root.join("backups");
    for directory in [&data, &tokens, &backups] {
        fs::create_dir_all(directory)?;
    }
    fs::write(backups.join("snapshot"), b"independent backup")?;
    let prepared = ErasureFiles::prepare(&data, &tokens, &locks, &backups)?;
    let redirect = tokens.join("late-redirect");
    symlink(&backups, &redirect)?;
    assert!(matches!(
        prepared.remove(),
        Err(ErasureFilesError::UnsafePath)
    ));
    assert!(!data.exists() && tokens.exists());
    fs::remove_file(redirect)?;
    ErasureFiles::prepare(&data, &tokens, &locks, &backups)?.remove()?;
    assert!(!data.exists() && !tokens.exists());
    assert_eq!(fs::read(backups.join("snapshot"))?, b"independent backup");
    Ok(())
}
