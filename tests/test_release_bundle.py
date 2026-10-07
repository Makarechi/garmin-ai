"""Verify the source-free installation candidate and its Compose graph."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("build_release_bundle", ROOT / "scripts/build_release_bundle.py")
bundle_module = module_from_spec(spec)
spec.loader.exec_module(bundle_module)
CONTENTS = bundle_module.CONTENTS
build_bundle = bundle_module.build_bundle
check_compose = bundle_module.check_compose
mount_spec = spec_from_file_location("release_mounts", ROOT / "scripts/release_mounts.py")
mount_module = module_from_spec(mount_spec)
mount_spec.loader.exec_module(mount_module)
verification_spec = spec_from_file_location(
    "verify_release_backup", ROOT / "scripts/verify_release_backup.py"
)
verification_module = module_from_spec(verification_spec)
verification_spec.loader.exec_module(verification_module)

IMAGE = "ghcr.io/example/garmin-ai@sha256:" + "a" * 64
SHA = "b" * 40


def test_release_compose_tracks_the_reviewed_service_layout():
    check_compose()
    content = (ROOT / "compose.release.yml").read_text()
    assert "build:" not in content
    assert content.count("${GA_APP_IMAGE:") == 3
    assert "service_completed_successfully" in content
    assert "127.0.0.1:${GA_API_PORT:-8080}:8080" in content


def test_bundle_contains_only_approved_public_files_and_pinned_images(tmp_path):
    archive, checksum = build_bundle(IMAGE, sha=SHA, output=tmp_path)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in checksum.read_text()
    with tarfile.open(archive) as bundle:
        names = {
            Path(name).relative_to(Path(name).parts[0]).as_posix() for name in bundle.getnames()
        }
        assert names == {*CONTENTS, "release.env", "release-manifest.json"}
        assert ".env" not in names
        assert "tokens" not in " ".join(names)
        manifest_name = next(
            name for name in bundle.getnames() if name.endswith("release-manifest.json")
        )
        manifest = json.load(bundle.extractfile(manifest_name))
        assert manifest["git_sha"] == SHA
        assert manifest["app_image"] == IMAGE
        assert manifest["database_image"].endswith(
            "@sha256:" + "bc8527e62f70f0766b29515077965025872fabb5349db421565f69ee273baf2d"
        )
        assert manifest["validated_platforms"] == []
        assert manifest["release_status"].startswith("candidate")
        env_name = next(name for name in bundle.getnames() if name.endswith("release.env"))
        assert bundle.extractfile(env_name).read() == f"GA_APP_IMAGE={IMAGE}\n".encode()
        for doc in ("telegram-pairing.md", "access-scopes.md", "operational-acceptance.md"):
            assert any(name.endswith("docs/" + doc) for name in bundle.getnames())


def test_release_compose_resolves_to_the_pinned_image_without_source(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    archive, _ = build_bundle(IMAGE, sha=SHA, output=tmp_path)
    with tarfile.open(archive) as bundle:
        bundle.extractall(tmp_path, filter="data")
    root = next(tmp_path.glob("garmin-ai-*/compose.release.yml")).parent
    (root / ".env").write_text(
        "GA_POSTGRES_PASSWORD=test-only-password\n"
        "GA_CONTAINER_DATABASE_URL=postgresql+psycopg://garmin:test-only-password@db:5432/garmin_ai\n"
        "GA_API_KEY=" + "x" * 40 + "\n"
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "--env-file",
            "release.env",
            "-f",
            "compose.release.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode and "docker: unknown command" in result.stderr:
        pytest.skip("Docker Compose plugin is unavailable")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert {services[name]["image"] for name in ("migrate", "api", "worker")} == {IMAGE}
    assert all("build" not in services[name] for name in ("migrate", "api", "worker"))
    assert services["db"]["image"].startswith("timescale/timescaledb:")


def test_update_mounts_preserve_old_storage_paths_without_exposing_secrets(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    fields = {
        "GA_DATA_DIR": old / "data",
        "GA_TOKEN_DIR": old / "tokens" / "garmin",
        "GA_BACKUP_DIR": old / "backups",
        "GA_LOCK_DIR": old / ".state",
    }
    env_file = new / ".env"
    env_file.write_text(
        "\n".join(f"{key}='{path}'" for key, path in fields.items())
        + "\nGA_API_KEY='test-only-key-must-not-appear'\n"
    )
    assert mount_module.preserved_mounts(env_file, new) == [str(old)]
    assert mount_module.preserved_mounts(new / "missing.env", new) == []
    env_file.write_text("GA_DATA_DIR='/tmp/unsafe,path'\n")
    with pytest.raises(ValueError, match="dedicated absolute path"):
        mount_module.preserved_mounts(env_file, new)


def test_update_mounts_accept_shallow_backup_and_lock_directories(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GA_BACKUP_DIR='/srv/backups'\nGA_LOCK_DIR='/srv/locks'\n")
    assert mount_module.preserved_mounts(env_file, tmp_path) == ["/srv/backups", "/srv/locks"]


def test_backup_verification_uses_separate_scratch_and_removes_plaintext(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backup-media"
    scratch = tmp_path / "local-scratch"
    backup_dir.mkdir()
    scratch.mkdir()
    source = backup_dir / "synthetic.enc"
    source.write_bytes(b"synthetic")
    seen = []

    def unpack(settings, encrypted, destination):
        seen.append((encrypted, destination))
        destination.mkdir()
        (destination / "database.jsonl.gz").write_bytes(b"synthetic")

    monkeypatch.setattr(
        verification_module, "Settings", lambda: SimpleNamespace(backup_dir=backup_dir)
    )
    monkeypatch.setattr(verification_module, "unpack_backup", unpack)
    monkeypatch.setattr("sys.argv", ["verify_release_backup.py", str(source)])
    verification_module.main(scratch=scratch)
    assert len(seen) == 1
    assert seen[0][0] == source
    assert seen[0][1].parent == scratch
    assert list(scratch.iterdir()) == []
    assert list(backup_dir.iterdir()) == [source]


def test_bundle_login_and_pairing_use_source_free_worker(tmp_path):
    archive, _ = build_bundle(IMAGE, sha=SHA, output=tmp_path)
    with tarfile.open(archive) as bundle:
        bundle.extractall(tmp_path, filter="data")
    root = next(tmp_path.glob("garmin-ai-*/install.sh")).parent
    (root / ".env").write_text("GA_API_KEY='synthetic-test-only'\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2 $3\" = 'compose version --short' ]; then echo v2.24.0; exit; fi\n"
        "if [ \"$1 $2\" = 'compose version' ]; then exit; fi\n"
        "printf '%s | image=%s | db=%s | data=%s | project=%s\\n' "
        '"$*" "${GA_APP_IMAGE:-}" "${GA_CONTAINER_DATABASE_URL:-}" '
        '"${GA_DATA_DIR:-}" "${COMPOSE_PROJECT_NAME:-}" >> "$DOCKER_CALLS"\n'
    )
    docker.chmod(0o755)
    calls = tmp_path / "docker-calls"
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_CALLS": str(calls),
        "GA_APP_IMAGE": "example/untrusted:latest",
        "GA_CONTAINER_DATABASE_URL": "synthetic-untrusted-database",
        "GA_DATA_DIR": "/synthetic/untrusted/data",
        "COMPOSE_PROJECT_NAME": "wrong-project",
    }
    for command in ("login", "pair-telegram"):
        result = subprocess.run(
            ["bash", "./install.sh", command], cwd=root, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert sum(" stop worker" in line for line in lines) == 2
    assert any(" run --rm --no-deps worker garmin-ai login" in line for line in lines)
    assert any(
        " run --rm --no-deps --workdir " in line
        and " worker garmin-ai pair-telegram --env-file " in line
        and " --container-runtime" in line
        for line in lines
    )
    assert all(f"image={IMAGE}" in line for line in lines if line.startswith("compose --env-file"))
    assert all(
        "db= | data= | project=" in line for line in lines if line.startswith("compose --env-file")
    )
