use std::{error::Error, fs, os::unix::fs::symlink};

use ga_archive::{
    erasure_files::{ErasureFiles, ErasureFilesError},
    storage_files::{StorageFilesError, validate_backup_layout},
};

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

#[test]
fn rejects_aliased_backups_and_maintenance_state_before_erasure() -> Result<(), Box<dyn Error>> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?.join("fixture");
    let data = root.join("Data");
    let tokens = root.join("Tokens");
    let locks = root.join("locks");
    let backups = root.join("backups");
    fs::create_dir_all(data.join("backups"))?;
    fs::create_dir_all(tokens.join("state"))?;
    let snapshot = data.join("backups/confirmed.enc");
    let lock = tokens.join("state/storage.lock");
    fs::write(&snapshot, b"confirmed backup")?;
    fs::write(&lock, b"maintenance state")?;
    let alias = root.join("DATA");
    if !alias.exists() {
        return Ok(());
    }

    for (tokens, locks, backups) in [
        (tokens.clone(), locks.clone(), alias.join("BACKUPS")),
        (tokens, root.join("TOKENS/STATE"), backups.clone()),
        (alias.clone(), locks.clone(), backups.clone()),
        (alias.join("missing"), locks, backups),
    ] {
        assert!(matches!(
            ErasureFiles::prepare(&data, &tokens, &locks, &backups),
            Err(ErasureFilesError::Overlap)
        ));
        assert!(matches!(
            validate_backup_layout(&data, &tokens, &locks, &backups),
            Err(StorageFilesError::Overlap)
        ));
    }
    assert_eq!(fs::read(&snapshot)?, b"confirmed backup");
    assert_eq!(fs::read(&lock)?, b"maintenance state");
    Ok(())
}

#[cfg(target_os = "linux")]
#[test]
#[ignore = "requires the disposable Linux bind-mount fixture"]
fn erases_mounted_roots_without_crossing_mounts() -> Result<(), Box<dyn Error>> {
    use std::{env, os::unix::fs::MetadataExt, path::Path};

    let scenario = env::var("GA_TEST_ERASURE_MOUNTS")?;
    let fixture = Path::new("/erasure-fixture");
    assert_eq!(
        fs::read(fixture.join("synthetic-only"))?,
        b"erasure mount test\n"
    );
    let root = if scenario == "mounted" {
        fixture.to_owned()
    } else {
        fixture.join(&scenario)
    };
    let data = root.join("data");
    let tokens = root.join("tokens");
    let locks = root.join("locks");
    let backups = root.join("backups");
    let snapshot = backups.join("confirmed.enc");
    assert_eq!(fs::read(&snapshot)?, b"synthetic backup\n");
    let current = data.join("raw.json");
    match scenario.as_str() {
        "mounted" => {
            let before = [fs::metadata(&data)?.ino(), fs::metadata(&tokens)?.ino()];
            ErasureFiles::prepare(&data, &tokens, &locks, &backups)?.remove()?;
            for (path, inode) in [(&data, before[0]), (&tokens, before[1])] {
                assert_eq!(fs::metadata(path)?.ino(), inode);
                assert!(fs::read_dir(path)?.next().is_none());
            }
            ErasureFiles::prepare(&data, &tokens, &locks, &backups)?.remove()?;
        }
        "nested" | "overlap" => {
            let nested = data.join("mounted");
            assert_eq!(fs::metadata(&nested)?.dev(), fs::metadata(&data)?.dev());
            let protected = nested.join("protected.txt");
            assert_eq!(fs::read(&protected)?, b"outside erasure\n");
            if scenario == "overlap" {
                assert_eq!(fs::metadata(&nested)?.ino(), fs::metadata(&backups)?.ino());
            }
            let result = ErasureFiles::prepare(&data, &tokens, &locks, &backups);
            assert!(match scenario.as_str() {
                "nested" => matches!(result, Err(ErasureFilesError::NestedMount)),
                _ => matches!(result, Err(ErasureFilesError::Overlap)),
            });
            assert_eq!(fs::read(protected)?, b"outside erasure\n");
            assert_eq!(fs::read(current)?, b"synthetic data\n");
        }
        _ => return Err("Unknown synthetic mount scenario".into()),
    }
    assert_eq!(fs::read(snapshot)?, b"synthetic backup\n");
    Ok(())
}
