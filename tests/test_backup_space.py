import base64
from types import SimpleNamespace

import pytest

from garmin_ai import backup_space as space
from garmin_ai.config import Settings
from garmin_ai.operations import create_backup


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_key=base64.urlsafe_b64encode(b"x" * 32).decode(),
    )


def test_shared_volume_accounts_for_simultaneous_export_tar_and_ciphertext(tmp_path, monkeypatch):
    config = settings(tmp_path)
    monkeypatch.setattr(space.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))
    result = space.backup_space(None, config, tmp_path / "backups" / "snapshot", export_bytes=100)
    archive = 100 + 4096 + 10240
    assert result["volumes"][0]["required_bytes"] == 100 + archive * 2 + 64 + space.RESERVE
    assert result["volumes"][0]["role"] == "shared"
    assert str(tmp_path) not in str(result)
    assert not config.data_dir.exists()


def test_low_space_prevents_plaintext_export(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai import operations

    config = settings(tmp_path)
    monkeypatch.setattr(space.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    monkeypatch.setattr(
        operations, "export_database", lambda *args: pytest.fail("Must fail before export")
    )
    destination = tmp_path / "backups" / "snapshot.enc"
    with pytest.raises(space.BackupSpaceInsufficient):
        create_backup(db_engine, config, destination)
    assert not destination.exists()
    assert not (config.data_dir / "backup-work").exists()


def test_preflight_uses_database_size_without_exporting_content(db, db_engine, tmp_path):
    result = space.backup_space(db_engine, settings(tmp_path), tmp_path / "snapshot")
    assert result["estimate_only"] and result["reserve_bytes"] == space.RESERVE
    assert set(result) == {"status", "estimate_only", "reserve_bytes", "volumes"}


@pytest.mark.parametrize(
    "local_free,remote_free,expected",
    [(10**12, 1, "insufficient"), (1, 10**12, "insufficient"), (10**12, 10**12, "ready")],
)
def test_separate_volume_capacity_is_checked_independently(
    tmp_path, monkeypatch, local_free, remote_free, expected
):
    config = settings(tmp_path)
    local = SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=1))
    remote = SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=2))
    monkeypatch.setattr(
        space,
        "existing_directory",
        lambda path: local if path == config.data_dir / "backup-work" else remote,
    )
    monkeypatch.setattr(
        space.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=local_free if path is local else remote_free),
    )
    result = space.backup_space(None, config, tmp_path / "snapshot", export_bytes=100)
    assert result["status"] == expected
    assert [v["role"] for v in result["volumes"]] == ["staging", "destination"]
