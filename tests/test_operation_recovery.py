"""Synthetic crash recovery and durability failure coverage."""

import ctypes
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from garmin_ai import archive, operations
from garmin_ai.jobs import claim, schedule_backup
from garmin_ai.models import Job


@pytest.mark.parametrize("failure", [None, "mount", "volume", "open", "flush"])
def test_windows_volume_barrier_closes_handle_and_propagates_failure(
    tmp_path, monkeypatch, failure
):
    handle = 2**40 + 17  # Detect accidental truncation to a 32-bit handle.

    def mount(path, output, size):
        assert path == str(tmp_path.absolute())
        output.value = "C:\\"
        return failure != "mount"

    def volume(path, output, size):
        assert path == "C:\\"
        output.value = "\\\\?\\Volume{synthetic}\\"
        return failure != "volume"

    kernel = SimpleNamespace(
        GetVolumePathNameW=Mock(side_effect=mount),
        GetVolumeNameForVolumeMountPointW=Mock(side_effect=volume),
        CreateFileW=Mock(return_value=ctypes.c_void_p(-1).value if failure == "open" else handle),
        FlushFileBuffers=Mock(return_value=failure != "flush"),
        CloseHandle=Mock(return_value=True),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic"), raising=False)
    if failure:
        with pytest.raises(OSError):
            archive.flush_windows_volume(tmp_path)
    else:
        archive.flush_windows_volume(tmp_path)
    if failure in (None, "flush"):
        kernel.CreateFileW.assert_called_once_with(
            "\\\\?\\Volume{synthetic}", 0x40000000, 3, None, 3, 0, None
        )
        kernel.FlushFileBuffers.assert_called_once_with(handle)
        kernel.CloseHandle.assert_called_once_with(handle)
        assert kernel.CreateFileW.restype == ctypes.c_void_p
    else:
        kernel.FlushFileBuffers.assert_not_called()
        kernel.CloseHandle.assert_not_called()


def test_directory_barrier_dispatches_to_windows_native_flush(tmp_path, monkeypatch):
    flush = Mock(side_effect=OSError("synthetic flush failure"))
    monkeypatch.setattr(archive, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(archive, "flush_windows_volume", flush)
    with pytest.raises(OSError, match="flush failure"):
        archive.fsync_directory(tmp_path)
    flush.assert_called_once_with(tmp_path)


def test_plaintext_crash_is_reclaimed_before_next_operation(tmp_path, monkeypatch):
    code = """
import os,sys
from pathlib import Path
from garmin_ai.operations import plaintext_workspace
with plaintext_workspace(Path(sys.argv[1])) as work:
    (work/'synthetic-token').write_text('synthetic-only')
    os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
    assert result.returncode == 17
    owned = tmp_path / ".garmin-ai-plaintext"
    stale = owned / "active" / "synthetic-token"
    assert stale.exists()
    unrelated = tmp_path / "tmp-unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("synthetic-only")
    original = operations.fsync_directory
    flushed = []

    def flush(path):
        if path == owned:
            assert not stale.exists()
            flushed.append(path)
        original(path)

    monkeypatch.setattr(operations, "fsync_directory", flush)
    with operations.plaintext_workspace(tmp_path) as work:
        assert flushed == [owned]
        assert list(work.iterdir()) == []
    assert flushed == [owned, owned]
    assert not (owned / "active").exists()
    assert (unrelated / "keep").read_text() == "synthetic-only"


def test_plaintext_recovery_does_not_remove_an_active_workspace(tmp_path):
    with operations.plaintext_workspace(tmp_path) as work:
        token = work / "synthetic"
        token.write_text("synthetic-only")
        with pytest.raises(BlockingIOError):
            with operations.plaintext_workspace(tmp_path):
                pytest.fail("An active operation must hold the recovery lock")
        assert token.read_text() == "synthetic-only"


def test_plaintext_recovery_failure_prevents_new_work_and_can_retry(tmp_path, monkeypatch):
    owned = tmp_path / ".garmin-ai-plaintext"
    stale = owned / "active"
    stale.mkdir(parents=True)
    (stale / "synthetic").write_text("synthetic-only")
    original = operations.fsync_directory

    def fail_flush(path):
        if path == owned:
            raise OSError("synthetic stale cleanup flush failure")
        original(path)

    monkeypatch.setattr(operations, "fsync_directory", fail_flush)
    with pytest.raises(OSError, match="stale cleanup"):
        with operations.plaintext_workspace(tmp_path):
            pytest.fail("Cleanup must be durable before starting new work")
    assert not stale.exists()
    monkeypatch.setattr(operations, "fsync_directory", original)
    with operations.plaintext_workspace(tmp_path) as work:
        assert list(work.iterdir()) == []


@pytest.mark.parametrize("junction", [False, True])
def test_plaintext_recovery_rejects_redirected_workspace(tmp_path, monkeypatch, junction):
    owned = tmp_path / ".garmin-ai-plaintext"
    owned.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("synthetic-only")
    active = owned / "active"
    if junction:
        active.mkdir()
        original = type(active).is_junction
        monkeypatch.setattr(type(active), "is_junction", lambda p: p == active or original(p))
    else:
        active.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(ValueError, match="redirected"):
        with operations.plaintext_workspace(tmp_path):
            pytest.fail("Recovery must not traverse redirected workspaces")
    assert (unrelated / "keep").read_text() == "synthetic-only"


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_daily_backup_gets_bounded_same_day_recovery_cycle(db, legacy):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    identity = schedule_backup(db, now)
    job = db.get(Job, identity)
    job.status = "failed"
    job.attempts = 8
    job.completed_at = None if legacy else now
    job.last_error = "OSError"
    db.flush()
    assert schedule_backup(db, now + timedelta(minutes=59)) is None
    later = now + timedelta(hours=1)
    assert schedule_backup(db, later) == identity
    db.refresh(job)
    assert job.status == "pending" and job.attempts == 0
    assert job.completed_at is None and job.last_error is None
    assert job.payload["date"] == "2026-09-08"
    assert job.payload["sync_wait_until"] == (later + timedelta(minutes=30)).isoformat()
    assert schedule_backup(db, later) is None
    assert claim(db, now=later, kinds=["backup"]).id == identity
    # Another exhausted cycle must wait for its own cooldown.
    job.status, job.attempts, job.completed_at = "failed", 8, later
    db.flush()
    assert schedule_backup(db, later + timedelta(minutes=59)) is None
    assert schedule_backup(db, later + timedelta(hours=1)) == identity


@pytest.mark.parametrize("status", ["pending", "running", "done"])
def test_daily_backup_recovery_preserves_nonfailed_jobs(db, status):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    identity = schedule_backup(db, now)
    job = db.get(Job, identity)
    job.status, job.attempts = status, 3
    job.completed_at = now
    db.flush()
    assert schedule_backup(db, now + timedelta(hours=2)) is None
    db.refresh(job)
    assert job.status == status and job.attempts == 3
    tomorrow = schedule_backup(db, now + timedelta(days=1))
    assert tomorrow != identity
    assert db.get(Job, tomorrow).payload["date"] == "2026-09-09"
