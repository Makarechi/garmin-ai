use std::{error::Error, fs, os::unix::fs::symlink};

use ga_archive::{
    ArchiveError,
    backup_space::{ExportSize, RESERVE_BYTES, VolumeRole, estimate_backup_space},
};

#[test]
fn preserves_staging_and_ciphertext_headroom() -> Result<(), Box<dyn Error>> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let data = root.join("data");
    let tokens = root.join("tokens");
    fs::create_dir_all(data.join("raw/nested"))?;
    fs::create_dir(&tokens)?;
    fs::write(data.join("raw/nested/object"), [1; 183])?;
    fs::write(data.join("coverage-report.json"), [2; 71])?;
    fs::write(tokens.join("fixture.txt"), [3; 99])?;
    let destination = root.join("new/destination/backup.ga");
    let export = 10_000_u64;
    let source = 183 + 71 + 99 + 4 * 4096 + 10240;
    let archive = export + source;
    let cipher = archive + archive / 100 + 64 * 1024 + 64;
    let estimate = estimate_backup_space(
        &data,
        &tokens,
        &destination,
        ExportSize::EstimatedBytes(export),
    )?;
    assert_eq!(estimate.volumes.len(), 1);
    assert_eq!(estimate.volumes[0].role, VolumeRole::Shared);
    assert_eq!(
        estimate.volumes[0].required_bytes,
        export + cipher + RESERVE_BYTES
    );
    let staged = estimate_backup_space(
        &data,
        &tokens,
        &destination,
        ExportSize::AlreadyStaged(export),
    )?;
    assert_eq!(staged.volumes[0].required_bytes, cipher + RESERVE_BYTES);
    assert!(!root.join("new").exists());
    assert!(matches!(
        estimate_backup_space(
            &data,
            &tokens,
            &destination,
            ExportSize::DatabaseBytes(u64::MAX)
        ),
        Err(ArchiveError::PayloadTooLarge)
    ));

    let redirect = data.join("raw/nested/redirect");
    symlink(&root, &redirect)?;
    assert!(matches!(
        estimate_backup_space(
            &data,
            &tokens,
            &destination,
            ExportSize::EstimatedBytes(export)
        ),
        Err(ArchiveError::InvalidObject)
    ));
    fs::remove_file(redirect)?;
    let redirect = root.join("alias");
    symlink(&root, &redirect)?;
    assert!(matches!(
        estimate_backup_space(
            &data,
            &tokens,
            &redirect.join("backup"),
            ExportSize::EstimatedBytes(export)
        ),
        Err(ArchiveError::InvalidRoot)
    ));
    Ok(())
}
