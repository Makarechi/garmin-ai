"""Synthetic host images and files: no private data or Docker daemon required."""

import fcntl
import importlib.util
import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_cleanup", ROOT / "deploy/gcp/release_cleanup.py"
)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
CURRENT, ROLLBACK, OLD, DB = ["sha256:" + c * 64 for c in "abcd"]


def row(identity, tag, repository="garmin-ai"):
    return {"id": identity, "repository": repository, "tag": tag}


def app(service, identity=CURRENT, **overrides):
    return {
        "id": service,
        "service": service,
        "image": identity,
        "status": "running",
        "health": "healthy",
        "exit_code": 0,
        **overrides,
    }


@pytest.fixture
def host(monkeypatch, tmp_path):
    images = {
        CURRENT: [row(CURRENT, "current")],
        ROLLBACK: [row(ROLLBACK, "rollback")],
        OLD: [row(OLD, "old"), row(OLD, "old-alias")],
        DB: [row(DB, "latest", "timescale/db")],
    }
    found = [app("api"), app("worker"), app("db", DB)]
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:3] == ("image", "inspect", "--format"):
            reference = args[-1]
            if reference == "verified":
                return ROLLBACK + "\n"
            return next(
                identity
                for identity, rows in images.items()
                if reference in [item["repository"] + ":" + item["tag"] for item in rows]
            )
        return ""

    monkeypatch.setattr(cleanup, "containers", lambda project=None: found)
    monkeypatch.setattr(cleanup, "image_references", lambda: images)
    monkeypatch.setattr(cleanup, "docker", docker)
    state = {
        "version": 1,
        "current": CURRENT,
        "rollback": ROLLBACK,
        "archive_dirs": [str(tmp_path)],
    }
    return state, images, found, calls


def archive(path, tags=None, *, manifest=True, old=True):
    with tarfile.open(path, "w:gz") as stream:
        payload = json.dumps(
            [
                {
                    "Config": "synthetic.json",
                    "Layers": [],
                    "RepoTags": tags or ["garmin-ai:synthetic"],
                }
            ]
        ).encode()
        member = tarfile.TarInfo("manifest.json" if manifest else "personal.json")
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))
    if old:
        os.utime(path, (time.time() - 172800,) * 2)


def test_preview_then_apply_keeps_current_rollback_database_and_backups(host, tmp_path):
    state, images, found, calls = host
    installer = tmp_path / "garmin-ai-old.tar.gz"
    archive(installer)
    private = tmp_path / "backups"
    private.mkdir()
    nested = private / "garmin-ai-old.tar.gz"
    archive(nested)
    encrypted = private / "daily.enc"
    encrypted.write_bytes(b"synthetic-ciphertext")
    backup_named_archive = tmp_path / "garmin-ai-backup.tar.gz"
    archive(backup_named_archive, manifest=False)
    other_installer = tmp_path / "garmin-ai-db.tar.gz"
    archive(other_installer, ["timescale/db:latest"])
    migration = app("migrate", OLD, status="exited", health=None)
    found.append(migration)

    preview = cleanup.cleanup(state)
    assert preview["images"] == [["garmin-ai:old", "garmin-ai:old-alias"]]
    assert preview["archives"] == [str(installer)]
    assert installer.exists() and calls == []
    result = cleanup.cleanup(state, apply=True)
    assert result["archive_bytes"] > 0 and not installer.exists()
    assert calls == [
        ("container", "rm", "migrate"),
        ("image", "inspect", "--format", "{{.Id}}", "garmin-ai:old"),
        ("image", "inspect", "--format", "{{.Id}}", "garmin-ai:old-alias"),
        ("image", "rm", "garmin-ai:old", "garmin-ai:old-alias"),
    ]
    assert nested.exists() and encrypted.read_bytes() == b"synthetic-ciphertext"
    assert backup_named_archive.exists() and other_installer.exists()


@pytest.mark.parametrize(
    "container",
    [
        app("migrate", OLD, status="exited", exit_code=1),
        app("other-project", OLD),
        app("debug", OLD, status="exited"),
    ],
)
def test_images_referenced_by_other_containers_are_preserved(host, container):
    state, _, found, calls = host
    found.append(container)
    assert cleanup.cleanup(state, apply=True)["images"] == []
    assert calls == []


def test_alias_from_another_repository_is_preserved(host):
    state, images, _, calls = host
    images[OLD].append(row(OLD, "keep", "unrelated"))
    assert cleanup.cleanup(state, apply=True)["images"] == []
    assert calls == []


@pytest.mark.parametrize("change", ["unhealthy", "different-image", "migration", "duplicate"])
def test_incomplete_or_unhealthy_deployment_prevents_all_deletion(host, change):
    state, _, found, calls = host
    if change == "unhealthy":
        found[1]["health"] = "starting"
    elif change == "different-image":
        found[1]["image"] = ROLLBACK
    elif change == "migration":
        found.append(app("migrate", OLD))
    else:
        found.append(app("api"))
    with pytest.raises(RuntimeError, match="healthy api and worker"):
        cleanup.cleanup(state, apply=True)
    assert calls == []


def test_failed_retained_image_check_prevents_deletion(host):
    state, images, _, calls = host
    del images[ROLLBACK]
    with pytest.raises(RuntimeError, match="retained image disappeared"):
        cleanup.cleanup(state, apply=True)
    assert calls == []


def test_initialize_explicit_rollback_and_advance_after_verified_rollout(host, tmp_path):
    state, images, _, _ = host
    with pytest.raises(RuntimeError, match="explicitly verified rollback"):
        cleanup.retention(None, CURRENT, images)
    assert cleanup.retention(None, CURRENT, images, "verified", [str(tmp_path)]) == state
    promoted = cleanup.retention(state, OLD, images)
    assert promoted["current"] == OLD and promoted["rollback"] == CURRENT
    reverted = cleanup.retention(state, ROLLBACK, images)
    assert reverted["current"] == ROLLBACK and reverted["rollback"] == CURRENT
    assert cleanup.retention(state, CURRENT, images) == state


def test_cannot_reinitialize_or_retain_the_current_image_twice(host):
    state, images, _, _ = host
    with pytest.raises(RuntimeError, match="only allowed"):
        cleanup.retention(state, CURRENT, images, "verified")
    with pytest.raises(RuntimeError, match="distinct"):
        cleanup.retention({**state, "rollback": CURRENT}, CURRENT, images)


@pytest.mark.parametrize("name", ["backups", "data", "tokens", "backup"])
def test_private_directories_cannot_be_configured_as_uploads(host, tmp_path, name):
    state, images, _, _ = host
    with pytest.raises(RuntimeError, match="outside private data"):
        cleanup.retention(state, CURRENT, images, archive_dirs=[str(tmp_path / name)])


def test_fresh_symlink_hardlink_corrupt_and_unknown_archives_are_preserved(tmp_path):
    fresh = tmp_path / "garmin-ai-fresh.tar.gz"
    archive(fresh, old=False)
    target = tmp_path / "actual.enc"
    archive(target)
    (tmp_path / "garmin-ai-symlink.tar.gz").symlink_to(target)
    os.link(target, tmp_path / "garmin-ai-hardlink.tar.gz")
    corrupt = tmp_path / "garmin-ai-corrupt.tar.gz"
    corrupt.write_bytes(b"invalid-gzip")
    os.utime(corrupt, (time.time() - 172800,) * 2)
    unknown = tmp_path / "unrelated.tar.gz"
    archive(unknown)
    assert list(cleanup.archive_candidates(str(tmp_path), time.time())) == []
    assert all(path.exists() for path in tmp_path.iterdir())


def test_archive_replacement_is_not_deleted(tmp_path):
    path = tmp_path / "garmin-ai-old.tar.gz"
    archive(path)
    original = path.stat()
    replacement = tmp_path / "new"
    replacement.write_bytes(b"replacement")
    replacement.replace(path)
    with pytest.raises(RuntimeError, match="Archive changed"):
        cleanup.remove_archive(path, original)
    assert path.read_bytes() == b"replacement"


def test_image_tag_replacement_stops_removal(host, monkeypatch):
    state, _, _, _ = host
    monkeypatch.setattr(cleanup, "docker", lambda *args: CURRENT)
    with pytest.raises(RuntimeError, match="Image tag changed"):
        cleanup.cleanup(state, apply=True)


def test_main_dry_run_has_no_state_and_record_only_is_private(host, tmp_path):
    _, _, _, calls = host
    directory = tmp_path / "state"
    args = [
        "--state-dir",
        str(directory),
        "--lock-file",
        str(tmp_path / "lock"),
        "--initialize-rollback",
        "verified",
        "--archive-dir",
        str(tmp_path),
    ]
    assert cleanup.main(args) == 0
    assert not (directory / "state.json").exists()
    assert cleanup.main([*args, "--apply", "--record-only"]) == 0
    assert (directory / "state.json").stat().st_mode & 0o777 == 0o600
    assert not any(args[:2] in {("image", "rm"), ("container", "rm")} for args in calls)
    assert cleanup.main(["--state-dir", str(directory), "--lock-file", str(tmp_path / "lock")]) == 0


def test_shared_lock_skips_cleanup(host, tmp_path, capsys):
    args = ["--state-dir", str(tmp_path / "state"), "--lock-file", str(tmp_path / "lock")]
    with open(tmp_path / "lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cleanup.main([*args, "--apply"]) == 0
    assert "lock is held" in capsys.readouterr().out


def test_timer_survives_reboot_and_uses_low_priority():
    timer = (ROOT / "deploy/gcp/garmin-ai-release-cleanup.timer").read_text()
    unit = (ROOT / "deploy/gcp/garmin-ai-release-cleanup.service").read_text()
    assert "Persistent=true" in timer and "12:00:00 UTC" in timer
    assert "Nice=15" in unit and "IOSchedulingClass=idle" in unit
    assert "--apply" in unit and "ProtectSystem=strict" in unit
