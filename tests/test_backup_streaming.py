import base64
import gzip
import io
import os
import tarfile

import pytest
from sqlalchemy import text

from garmin_ai import backup_space, operations
from garmin_ai.config import Settings


def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_dir=tmp_path / "backups",
        backup_key=base64.urlsafe_b64encode(b"x" * 32).decode(),
    )


@pytest.mark.parametrize("compressible", [False, True])
def test_streamed_backup_size_staging_and_complete_recovery(
    db, db_engine, tmp_path, monkeypatch, compressible
):
    config = settings(tmp_path)
    raw = config.data_dir / "raw"
    raw.mkdir(parents=True)
    config.token_dir.mkdir()
    content = b'{"synthetic": true}\n' * 100_000 if compressible else os.urandom(2_000_000)
    (raw / "synthetic").write_bytes(content)
    (config.token_dir / "synthetic-token").write_bytes(b"synthetic token")
    (config.data_dir / "coverage-report.json").write_bytes(b'{"synthetic": true}')
    destination = config.backup_dir / "snapshot.enc"
    original_verify = operations.verify_encrypted_file
    checked = []

    def verify(path, key):
        work = config.data_dir / "backup-work" / ".garmin-ai-plaintext" / "active"
        assert [p.name for p in work.iterdir()] == ["database.jsonl.gz"]
        assert not destination.exists()
        report = backup_space.backup_space(
            db_engine,
            config,
            destination,
            export_bytes=(work / "database.jsonl.gz").stat().st_size,
            export_staged=True,
        )
        assert path.stat().st_size < report["volumes"][0]["required_bytes"] - backup_space.RESERVE
        checked.append(path.stat().st_size)
        return original_verify(path, key)

    monkeypatch.setattr(operations, "verify_encrypted_file", verify)
    counts = operations.create_backup(db_engine, config, destination)
    assert len(checked) == 1
    if compressible:
        assert destination.stat().st_size < len(content) // 10
    assert not (config.data_dir / "backup-work" / ".garmin-ai-plaintext" / "active").exists()
    compressed = tmp_path / "authenticated.tar.gz"
    operations.decrypt_file(destination, compressed, operations.backup_key(config))
    assert compressed.read_bytes()[:2] == b"\x1f\x8b"
    recovered = tmp_path / "recovered"
    operations.unpack_backup(config, destination, recovered)
    assert (recovered / "raw/synthetic").read_bytes() == content
    assert (recovered / "tokens/synthetic-token").read_bytes() == b"synthetic token"
    assert (recovered / "coverage-report.json").read_bytes() == b'{"synthetic": true}'
    with gzip.open(recovered / "database.jsonl.gz", "rt") as stream:
        assert '"format": "garmin-ai-jsonl-v1"' in stream.readline()
    names = ", ".join('"' + table.name + '"' for table in operations.Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    assert operations.restore_database(db_engine, recovered / "database.jsonl.gz") == counts


def test_legacy_uncompressed_backup_remains_recoverable(tmp_path):
    config = settings(tmp_path)
    source = tmp_path / "legacy.tar"
    contents = {
        "database.jsonl.gz": gzip.compress(b'{"synthetic": true}\n'),
        "raw/synthetic.json": b'{"synthetic": true}',
        "tokens/synthetic": b"synthetic token",
    }
    with tarfile.open(source, "w") as archive:
        for name, content in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    destination = config.backup_dir / "legacy.enc"
    operations.encrypt_file(source, destination, operations.backup_key(config))
    recovered = tmp_path / "recovered"
    operations.unpack_backup(config, destination, recovered)
    for name, content in contents.items():
        assert (recovered / name).read_bytes() == content


@pytest.mark.parametrize("failure", ["archive", "verification", "disk"])
def test_stream_failure_preserves_last_backup_and_removes_partial_output(
    db, db_engine, tmp_path, monkeypatch, failure
):
    config = settings(tmp_path)
    previous = config.backup_dir / "garmin-ai-2026-10-20.enc"
    operations.scheduled_backup(db_engine, config, previous)
    saved = previous.read_bytes()
    destination = config.backup_dir / "garmin-ai-2026-10-21.enc"
    config.backup_keep_daily = 1

    def fail(*args, **kwargs):
        raise OSError("synthetic streaming failure")

    if failure == "archive":
        original_add = tarfile.TarFile.add

        def add_then_fail(*args, **kwargs):
            original_add(*args, **kwargs)
            fail()

        monkeypatch.setattr(tarfile.TarFile, "add", add_then_fail)
    elif failure == "verification":
        monkeypatch.setattr(operations, "verify_encrypted_file", fail)
    else:
        monkeypatch.setattr(operations._EncryptingWriter, "write", fail)
    with pytest.raises(OSError, match="synthetic streaming failure"):
        operations.scheduled_backup(db_engine, config, destination)
    assert list(config.backup_dir.iterdir()) == [previous]
    assert previous.read_bytes() == saved
    assert not (config.data_dir / "backup-work" / ".garmin-ai-plaintext" / "active").exists()
