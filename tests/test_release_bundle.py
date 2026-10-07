"""Verify the source-free installation candidate and its Compose graph."""

import hashlib
import json
import shutil
import subprocess
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("build_release_bundle", ROOT / "scripts/build_release_bundle.py")
bundle_module = module_from_spec(spec)
spec.loader.exec_module(bundle_module)
CONTENTS = bundle_module.CONTENTS
build_bundle = bundle_module.build_bundle
check_compose = bundle_module.check_compose

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
