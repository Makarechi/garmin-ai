"""Verify the source-free installation candidate and its Compose graph."""

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
from contextlib import nullcontext
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

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
inspect_spec = spec_from_file_location(
    "verify_release_install", ROOT / "scripts/verify_release_install.py"
)
inspect_module = module_from_spec(inspect_spec)
inspect_spec.loader.exec_module(inspect_module)

IMAGE = "ghcr.io/example/garmin-ai@sha256:" + "a" * 64
SHA = "b" * 40


def test_inspection_retries_connection_reset_while_the_api_starts(monkeypatch):
    paths = []

    def request(_url, path, _key, _body=None):
        paths.append(path)
        if path == "/health/ready":
            if len(paths) == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            return {"status": "ready"}
        if path == "/tools":
            return [{"name": "events"}]
        assert path == "/events"
        raise HTTPError(path, 404, "Not Found", {}, None)

    process = SimpleNamespace(pid=1234, poll=lambda: None, wait=lambda **_options: 0)
    monkeypatch.setattr(
        inspect_module,
        "subprocess",
        SimpleNamespace(
            Popen=lambda *_args, **_options: nullcontext(process),
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    monkeypatch.setattr(inspect_module, "dotenv_values", lambda _path: {"GA_API_KEY": "synthetic"})
    monkeypatch.setattr(inspect_module, "request", request)
    monkeypatch.setattr(inspect_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(inspect_module, "os", SimpleNamespace(killpg=lambda *_args: None))
    inspect_module.inspect_bundle("http://127.0.0.1:8080", ["./install.sh"])
    assert paths == ["/health/ready", "/health/ready", "/tools", "/events"]


def native_config(revision, entrypoint=None):
    return {
        "os": "linux",
        "config": {
            "Labels": {bundle_module.REVISION_LABEL: revision} if revision else None,
            "Entrypoint": entrypoint or ["garmin-ai-rust"],
        },
    }


@pytest.mark.parametrize(
    ("revision", "entrypoint", "error"),
    [
        (SHA, None, None),
        ("c" * 40, None, "revision does not match"),
        (None, None, "revision does not match"),
        (SHA, ["garmin-ai"], "must start the native"),
    ],
)
def test_native_bundle_checks_every_runnable_image(
    tmp_path, monkeypatch, revision, entrypoint, error
):
    first = "sha256:" + "1" * 64
    second = "sha256:" + "2" * 64
    repository = IMAGE.split("@")[0]
    inspected = []

    def document(image, *options):
        if image == IMAGE:
            assert options == ("--raw",)
            return {
                "manifests": [
                    {"digest": second, "platform": {"os": "linux", "architecture": "arm64"}},
                    {"digest": first, "platform": {"os": "linux", "architecture": "amd64"}},
                    {
                        "digest": "sha256:" + "3" * 64,
                        "platform": {"os": "unknown", "architecture": "unknown"},
                        "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                    },
                ]
            }
        if options == ("--raw",):
            return {"schemaVersion": 2}
        assert image in {f"{repository}@{first}", f"{repository}@{second}"}
        inspected.append(image)
        return native_config(SHA) if image.endswith(first) else native_config(revision, entrypoint)

    monkeypatch.setattr(bundle_module, "registry_document", document)
    output = tmp_path / "bundle"
    if error is None:
        archive, _ = build_bundle(IMAGE, sha=SHA, output=output, runtime="rust")
        assert archive.is_file()
    else:
        with pytest.raises(ValueError, match=error):
            build_bundle(IMAGE, sha=SHA, output=output, runtime="rust")
        assert not output.exists()
    assert inspected == [f"{repository}@{first}", f"{repository}@{second}"]


def test_registry_manifest_bytes_match_the_requested_digest(monkeypatch):
    body = b'{"schemaVersion":2}'

    def run(command, **kwargs):
        assert command[:5] == ["docker", "buildx", "imagetools", "inspect", "--raw"]
        return SimpleNamespace(stdout=body)

    monkeypatch.setattr(bundle_module.subprocess, "run", run)
    verified = IMAGE.split("@sha256:")[0] + "@sha256:" + hashlib.sha256(body).hexdigest()
    assert bundle_module.registry_document(verified, "--raw") == {"schemaVersion": 2}
    with pytest.raises(ValueError, match="differs from the requested image digest"):
        bundle_module.registry_document(IMAGE, "--raw")


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
        for doc in (
            "telegram-pairing.md",
            "access-scopes.md",
            "operational-acceptance.md",
            "community-pilot-kit.md",
        ):
            assert any(name.endswith("docs/" + doc) for name in bundle.getnames())


def test_bundled_markdown_links_resolve_within_bundle():
    included = set(CONTENTS)
    for name in CONTENTS:
        if not name.endswith(".md"):
            continue
        for target in re.findall(r"\]\(([^)]+)\)", (ROOT / name).read_text()):
            path = target.split("#", 1)[0]
            if not path or not path.endswith(".md") or "://" in path:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
            assert resolved in included, f"{name} links to missing {resolved}"


@pytest.mark.parametrize(("runtime", "gcp"), [("python", False), ("rust", False), ("rust", True)])
def test_release_compose_resolves_to_the_pinned_image_without_source(
    tmp_path, monkeypatch, runtime, gcp
):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    if runtime == "rust":
        monkeypatch.setattr(
            bundle_module,
            "registry_document",
            lambda _image, *options: {} if options == ("--raw",) else native_config(SHA),
        )
    archive, checksum = build_bundle(IMAGE, sha=SHA, output=tmp_path, runtime=runtime)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in checksum.read_text()
    with tarfile.open(archive) as bundle:
        bundle.extractall(tmp_path, filter="data")
    root = next(tmp_path.glob("garmin-ai-*/compose.release.yml")).parent
    (root / ".env").write_text(
        "GA_POSTGRES_PASSWORD=test-only-password\n"
        "GA_CONTAINER_DATABASE_URL=postgresql+psycopg://garmin:test-only-password@db:5432/garmin_ai\n"
        "GA_API_KEY=" + "x" * 40 + "\n"
    )
    override = ["-f", "compose.gcp.yml"] if gcp else []
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
            *override,
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
    if runtime == "rust":
        # The temporary initialization server only accepts Unix socket connections.
        assert services["db"]["healthcheck"]["test"] == [
            "CMD",
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "garmin",
            "-d",
            "garmin_ai",
        ]
        assert "GA_APP_RUNTIME=rust\n" in (root / "release.env").read_text()
        assert json.loads((root / "release-manifest.json").read_text())["runtime"] == "rust"
        assert not list(root.rglob("*.py"))
        assert services["migrate"]["command"] == ["migrate"]
        assert services["worker"]["command"] == ["worker"]
        assert services["api"]["healthcheck"]["test"] == [
            "CMD",
            "garmin-ai-rust",
            "healthcheck",
            "--ready",
        ]
        assert services["worker"]["healthcheck"]["test"] == [
            "CMD",
            "garmin-ai-rust",
            "healthcheck",
            "--worker",
        ]
        for name in ("migrate", "api", "worker"):
            state = next(
                path for path in services[name]["volumes"] if path["target"] == "/app/state"
            )
            assert state["source"] == str(root / ".state")
            assert state["bind"]["create_host_path"] is False
    if gcp:
        assert "auth-gateway" not in services
        assert services["db"]["mem_limit"] == str(320 * 1024**2)
        assert services["db"]["environment"]["NO_TS_TUNE"] == "true"
        for service in services.values():
            assert service["logging"]["options"] == {"max-file": "2", "max-size": "5m"}
        assert os.access(root / "deploy/gcp/bootstrap.sh", os.X_OK)
        assert os.access(root / "deploy/gcp/install-native-cleanup.sh", os.X_OK)
        with (root / ".env").open("a") as settings:
            settings.write(
                "COMPOSE_PROFILES=garmin-web-auth\nGA_GARMIN_AUTH_HOST=synthetic.example\n"
            )
        gateway = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                ".env",
                "--env-file",
                "release.env",
                "-f",
                "compose.release.yml",
                *override,
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        enabled = json.loads(gateway.stdout)["services"]["auth-gateway"]
        assert enabled["environment"]["GA_GARMIN_AUTH_HOST"] == "synthetic.example"
        assert enabled["depends_on"]["api"]["condition"] == "service_healthy"
        assert [port["target"] for port in enabled["ports"]] == [80, 443]
        caddyfile = next(
            volume for volume in enabled["volumes"] if volume["target"] == "/etc/caddy/Caddyfile"
        )
        assert caddyfile["read_only"] is True
        assert Path(caddyfile["source"]).is_file()


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
