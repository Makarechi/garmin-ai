"""Verify the source-free installation candidate and its Compose graph."""

import errno
import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
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


def native_config(revision, entrypoint=None, *, architecture="amd64"):
    return {
        "os": "linux",
        "architecture": architecture,
        "config": {
            "Labels": {bundle_module.REVISION_LABEL: revision} if revision else None,
            "Entrypoint": entrypoint or ["garmin-ai-rust"],
        },
    }


@pytest.mark.parametrize(
    ("revision", "entrypoint", "architecture", "error"),
    [
        (SHA, None, "arm64", None),
        ("c" * 40, None, "arm64", "revision does not match"),
        (None, None, "arm64", "revision does not match"),
        (SHA, ["garmin-ai"], "arm64", "must start the native"),
        (SHA, None, "amd64", "platform does not match its index"),
    ],
)
def test_native_bundle_checks_every_runnable_image(
    tmp_path, monkeypatch, revision, entrypoint, architecture, error
):
    first = "sha256:" + "1" * 64
    second = "sha256:" + "2" * 64
    first_config = "sha256:" + "4" * 64
    second_config = "sha256:" + "5" * 64
    nested = "sha256:" + "6" * 64
    repository = IMAGE.split("@")[0]
    inspected = []

    def document(image, *options):
        if image == IMAGE:
            assert options == ("--raw",)
            return {
                "manifests": [
                    {"digest": nested, "platform": {"os": "linux", "architecture": "arm64"}},
                    {"digest": first, "platform": {"os": "linux", "architecture": "amd64"}},
                    {
                        "digest": "sha256:" + "3" * 64,
                        "platform": {"os": "unknown", "architecture": "unknown"},
                        "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                    },
                ]
            }
        if image.endswith(nested):
            assert options == ("--raw",)
            return {"manifests": [{"digest": second}]}
        if options == ("--raw",):
            return {"config": {"digest": first_config if image.endswith(first) else second_config}}
        assert image in {f"{repository}@{first}", f"{repository}@{second}"}
        inspected.append(image)
        if image.endswith(first):
            return native_config(SHA)
        return native_config(revision, entrypoint, architecture=architecture)

    monkeypatch.setattr(bundle_module, "registry_document", document)
    output = tmp_path / "bundle"
    if error is None:
        archive, _ = build_bundle(IMAGE, sha=SHA, output=output, runtime="rust")
        with tarfile.open(archive) as bundle:
            manifest = json.load(
                bundle.extractfile(
                    next(
                        name
                        for name in bundle.getnames()
                        if name.endswith("/release-manifest.json")
                    )
                )
            )
        assert manifest["app_platform_images"] == {
            "linux/amd64": {"manifest_digest": first, "config_digest": first_config},
            "linux/arm64": {"manifest_digest": second, "config_digest": second_config},
        }
        assert manifest["validated_platforms"] == []
    else:
        with pytest.raises(ValueError, match=error):
            build_bundle(IMAGE, sha=SHA, output=output, runtime="rust")
        assert not output.exists()
    assert inspected == [f"{repository}@{first}", f"{repository}@{second}"]


@pytest.mark.parametrize("second_image", [False, True])
def test_native_bundle_rejects_ambiguous_platforms_but_allows_single_image(
    tmp_path, monkeypatch, second_image
):
    first = "sha256:" + "1" * 64
    second = "sha256:" + "2" * 64
    config = "sha256:" + "3" * 64

    def document(image, *options):
        if options != ("--raw",):
            return native_config(SHA)
        if image == IMAGE and second_image:
            platform = {"os": "linux", "architecture": "amd64"}
            return {
                "manifests": [
                    {"digest": digest, "platform": platform} for digest in [first, second]
                ]
            }
        return {"config": {"digest": config}}

    monkeypatch.setattr(bundle_module, "registry_document", document)
    if second_image:
        with pytest.raises(ValueError, match="multiple images for linux/amd64"):
            build_bundle(IMAGE, sha=SHA, output=tmp_path / "bundle", runtime="rust")
        assert not (tmp_path / "bundle").exists()
    else:
        assert bundle_module.verify_native_image(IMAGE, SHA) == {
            "linux/amd64": {"manifest_digest": IMAGE.rsplit("@", 1)[1], "config_digest": config}
        }


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


@pytest.fixture
def native_version_reports(tmp_path, monkeypatch):
    known_images = {
        f"linux/{arch}": {
            "manifest_digest": f"sha256:{digest * 64}",
            "config_digest": f"sha256:{digest * 63}0",
        }
        for arch, digest in (("amd64", "b"), ("arm64", "c"))
    }
    monkeypatch.setattr(bundle_module, "verify_native_image", lambda *_args: known_images)
    release_info = {
        "application_version": "0.1.0",
        "database_revision": bundle_module.REVISION,
        "archive_json": "Python-compatible UTF-8 JSON",
        "fit_parser_version": 9,
        "garmin_parser_version": 9,
        "fit_profile_version": "21.171",
        "database_export_format": "garmin-ai-jsonl-v1",
        "backup_envelope": "GARMINAI1",
    }
    reports = []
    for platform in ("linux/amd64", "linux/arm64/v8"):
        parts = platform.split("/")
        document = {
            "runtime": "rust",
            "git_sha": SHA,
            "app_image": IMAGE,
            "platform": platform,
            "app_platform_image": known_images["/".join(parts[:2])],
            "native_runtime_processes_verified": True,
            "portable_database_export_verified": True,
            "native_backup_and_recovery_verified": True,
            "release_info": release_info,
            "private_example": "should-not-enter-the-release-archive",
        }
        report = tmp_path / f"{parts[1]}.json"
        report.write_text(json.dumps(document))
        reports.append(report)
    return tuple(reports), release_info


def test_native_bundle_attaches_versions_for_exact_images(tmp_path, native_version_reports):
    reports, release_info = native_version_reports
    archive, checksum = build_bundle(
        IMAGE,
        sha=SHA,
        output=tmp_path / "output",
        runtime="rust",
        native_installation_reports=reports[::-1],
    )
    with tarfile.open(archive) as release:
        files = [name for name in release.getnames() if name.endswith("release-manifest.json")]
        (manifest_file,) = files
        raw = release.extractfile(manifest_file).read()
        assert b"should-not-enter-the-release-archive" not in raw
        manifest = json.loads(raw)
    assert manifest["release_info"] == release_info
    assert manifest["native_installation_report_sha256"] == {
        f"linux/{path.stem}": [hashlib.sha256(path.read_bytes()).hexdigest()] for path in reports
    }
    assert manifest["validated_platforms"] == []
    assert manifest["release_status"].startswith("candidate")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in checksum.read_text()


@pytest.mark.parametrize(
    ("variation", "error"),
    [
        ("other_revision", "does not match the release image"),
        ("other_image", "does not match the release image"),
        ("other_platform_image", "does not match the release image"),
        ("failed_backup", "missing storage verification"),
        ("unknown_version_field", "invalid release versions"),
        ("wrong_database_revision", "invalid release versions"),
        ("different_parser", "disagree on release versions"),
        ("missing_platform", "must cover every image platform"),
        ("missing_file", "Could not read native installation report"),
    ],
)
def test_native_bundle_rejects_incompatible_version_reports(
    tmp_path, native_version_reports, variation, error
):
    reports, _ = native_version_reports
    receipt = reports[1]
    document = json.loads(receipt.read_text())
    if variation == "other_revision":
        document["git_sha"] = "d" * 40
    elif variation == "other_image":
        document["app_image"] = IMAGE.replace("example", "different")
    elif variation == "other_platform_image":
        document["app_platform_image"]["config_digest"] = "sha256:" + "d" * 64
    elif variation == "failed_backup":
        document["native_backup_and_recovery_verified"] = False
    elif variation == "unknown_version_field":
        document["release_info"]["unknown"] = "synthetic-secret-marker"
    elif variation == "wrong_database_revision":
        document["release_info"]["database_revision"] = "a" * 12
    elif variation == "different_parser":
        document["release_info"]["garmin_parser_version"] += 1
    elif variation == "missing_platform":
        reports = reports[:1]
    elif variation == "missing_file":
        reports = (reports[0], tmp_path / "missing.json")
    receipt.write_text(json.dumps(document))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match=error):
        build_bundle(
            IMAGE, sha=SHA, output=output, runtime="rust", native_installation_reports=reports
        )
    assert not output.exists()


def test_python_bundle_cannot_attach_native_versions(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="require the Rust runtime"):
        build_bundle(
            IMAGE,
            sha=SHA,
            output=output,
            native_installation_reports=(tmp_path / "receipt.json",),
        )
    assert not output.exists()


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


@pytest.mark.parametrize("failure", ["archive_write", "checksum_publish"])
def test_bundle_rebuild_keeps_previous_artifacts_after_storage_failure(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(bundle_module, "verify_native_image", lambda *_args: {})
    archive, checksum = build_bundle(IMAGE, sha=SHA, output=tmp_path, runtime="rust")
    previous = archive.read_bytes(), checksum.read_bytes()
    replacement = IMAGE.split("@")[0] + "@sha256:" + "c" * 64
    add = bundle_module._add
    replace = Path.replace

    def fail_during_archive(tar, name, data, mode):
        add(tar, name, data, mode)
        raise OSError(errno.ENOSPC, "synthetic volume full")

    def fail_when_publishing_checksum(path, target):
        if target == checksum:
            raise OSError(errno.ENOSPC, "synthetic volume full")
        return replace(path, target)

    with monkeypatch.context() as patch:
        if failure == "archive_write":
            patch.setattr(bundle_module, "_add", fail_during_archive)
        else:
            patch.setattr(Path, "replace", fail_when_publishing_checksum)
        with pytest.raises(OSError) as failed:
            build_bundle(replacement, sha=SHA, output=tmp_path, runtime="rust")
        assert failed.value.errno == errno.ENOSPC

    assert set(tmp_path.iterdir()) == {archive, checksum}
    assert (archive.read_bytes(), checksum.read_bytes()) == previous

    assert build_bundle(replacement, sha=SHA, output=tmp_path, runtime="rust") == (
        archive,
        checksum,
    )
    assert (archive.read_bytes(), checksum.read_bytes()) != previous
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_text() == f"{digest}  {archive.name}\n"


@pytest.mark.parametrize("interruption", ["archive", "checksum"])
def test_rebuild_recovers_after_interrupted_publication(tmp_path, monkeypatch, interruption):
    monkeypatch.setattr(bundle_module, "verify_native_image", lambda *_args: {})
    archive, checksum = build_bundle(IMAGE, sha=SHA, output=tmp_path, runtime="rust")
    original = archive.read_bytes(), checksum.read_bytes()
    replacement = IMAGE.split("@")[0] + "@sha256:" + "c" * 64
    child = """
import os
import signal
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

builder, output, revision, image, stop_at = sys.argv[1:]
spec = spec_from_file_location("release_builder", builder)
module = module_from_spec(spec)
spec.loader.exec_module(module)
module.verify_native_image = lambda *_: {}
replace = Path.replace

def replace_then_kill(path, target):
    result = replace(path, target)
    if target.name == stop_at:
        os.kill(os.getpid(), signal.SIGKILL)
    return result

Path.replace = replace_then_kill
module.build_bundle(image, sha=revision, output=Path(output), runtime="rust")
"""
    interrupted = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(ROOT / "scripts/build_release_bundle.py"),
            str(tmp_path),
            SHA,
            replacement,
            (archive if interruption == "archive" else checksum).name,
        ],
        timeout=20,
        check=False,
    )
    assert interrupted.returncode == -signal.SIGKILL
    expected = (
        original if interruption == "archive" else (archive.read_bytes(), checksum.read_bytes())
    )

    def full_volume(*_args):
        raise OSError(errno.ENOSPC, "synthetic volume full during retry")

    with monkeypatch.context() as patch:
        patch.setattr(bundle_module, "_add", full_volume)
        with pytest.raises(OSError) as failed:
            build_bundle(replacement, sha=SHA, output=tmp_path, runtime="rust")
        assert failed.value.errno == errno.ENOSPC
    assert (archive.read_bytes(), checksum.read_bytes()) == expected
    assert (
        checksum.read_text()
        == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )


@pytest.mark.parametrize("contents", [CONTENTS, bundle_module.NATIVE_CONTENTS])
def test_bundled_markdown_links_resolve_within_bundle(contents):
    included = set(contents)
    for name in contents:
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
            lambda _image, *options: (
                {"config": {"digest": "sha256:" + "c" * 64}}
                if options == ("--raw",)
                else native_config(SHA)
            ),
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
        assert (root / "migration/cutover-runbook.md").is_file()
        assert (root / "migration/rollback-runbook.md").is_file()
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


@pytest.mark.parametrize("runtime", ["python", "rust"])
def test_bundle_account_commands_use_source_free_worker(tmp_path, monkeypatch, runtime):
    monkeypatch.setattr(bundle_module, "verify_native_image", lambda *_args: {})
    archive, _ = build_bundle(IMAGE, sha=SHA, output=tmp_path, runtime=runtime)
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
        'case "$*" in *" run "*" enroll-account "*) exit "${ENROLL_STATUS:-0}";; esac\n'
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

    def install(*args, **overrides):
        return subprocess.run(
            ["bash", "./install.sh", *args],
            cwd=root,
            env={**env, **overrides},
            capture_output=True,
            text=True,
        )

    for args in ((), ("--help",), ("--confirm-existing-owner", "extra")):
        result = install("enroll-account", *args)
        assert result.returncode == 2
        assert "enroll-account requires --confirm-existing-owner" in result.stderr
        assert not calls.exists()

    for command in ("login", "pair-telegram"):
        result = install(command)
        assert result.returncode == 0, result.stderr
    result = install("enroll-account", "--confirm-existing-owner")
    assert result.returncode == 0, result.stderr
    result = install("enroll-account", "--confirm-existing-owner", ENROLL_STATUS="1")
    assert result.returncode == 1
    assert "./install.sh start" in result.stderr
    lines = calls.read_text().splitlines()
    entry = "garmin-ai " if runtime == "python" else ""
    assert any(f" run --rm --no-deps worker {entry}login" in line for line in lines)
    assert any(
        " run --rm --no-deps --workdir " in line
        and f" worker {entry}pair-telegram --env-file " in line
        and " --container-runtime" in line
        for line in lines
    )
    assert len(lines) == 8
    assert all(" stop worker | " in line for line in lines[::2])
    assert all(
        f" run --rm --no-deps -T worker {entry}enroll-account --confirm-existing-owner | " in line
        for line in lines[5::2]
    )
    assert all(f"image={IMAGE}" in line for line in lines if line.startswith("compose --env-file"))
    assert all(
        "db= | data= | project=" in line for line in lines if line.startswith("compose --env-file")
    )


@pytest.mark.parametrize("runtime", ["python", "rust"])
@pytest.mark.parametrize("unpack_status", [0, 1])
def test_backup_uses_worker_identity_and_removes_verification_volume(
    tmp_path, runtime, unpack_status
):
    shutil.copy2(ROOT / "install.sh", tmp_path / "install.sh")
    (tmp_path / "release.env").write_text(f"GA_APP_IMAGE={IMAGE}\nGA_APP_RUNTIME={runtime}\n")
    worker_identity = f"{os.getuid() + 1}:{os.getgid() + 1}"
    (tmp_path / ".env").write_text(f"GA_APP_UID={os.getuid() + 1}\nGA_APP_GID={os.getgid() + 1}\n")
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/bin/sh
case "$*" in
    'compose version --short') echo 'v2.24.0'; exit 0 ;;
    'compose version') exit 0 ;;
esac
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "$*" in
    *'--entrypoint sh worker'*) printf '%s\n' "$WORKER_IDENTITY" ;;
    'volume create'*) printf '%s\n' "$VERIFICATION_VOLUME" ;;
    *' worker unpack-backup '*|*' python /setup/scripts/verify_release_backup.py '*)
        exit "$UNPACK_STATUS" ;;
esac
"""
    )
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    scratch = "a" * 64
    result = subprocess.run(
        ["bash", "./install.sh", "backup"],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "DOCKER_CALLS": str(calls),
            "WORKER_IDENTITY": worker_identity,
            "VERIFICATION_VOLUME": scratch,
            "UNPACK_STATUS": str(unpack_status),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == unpack_status, result.stderr
    recorded = calls.read_text().splitlines()
    assert [command for command in recorded if "--entrypoint chown" in command] == [
        f"run --rm --network none --user 0 -v {scratch}:/verification --entrypoint chown "
        f"{IMAGE} {worker_identity} /verification"
    ]
    assert [command for command in recorded if command.startswith("volume ")] == [
        "volume create",
        f"volume rm {'-f ' if unpack_status else ''}{scratch}",
    ]
    assert ("Verified encrypted backup:" in result.stdout) == (unpack_status == 0)


@pytest.mark.parametrize(
    ("assignments", "expected_status"),
    [
        (f"GA_APP_IMAGE={IMAGE}\n", 0),
        (f"GA_APP_IMAGE={IMAGE}", 0),
        (f"GA_APP_IMAGE={IMAGE}\nGA_APP_IMAGE={IMAGE}\n", 1),
        (f"GA_APP_IMAGE={IMAGE}\nGA_APP_IMAGE=\n", 1),
        (f"GA_APP_IMAGE=\nGA_APP_IMAGE={IMAGE}\n", 1),
    ],
)
def test_installer_requires_one_release_image_assignment(tmp_path, assignments, expected_status):
    shutil.copy2(ROOT / "install.sh", tmp_path / "install.sh")
    (tmp_path / "release.env").write_text(assignments)
    (tmp_path / ".env").touch()
    docker = tmp_path / "docker"
    docker.write_text(
        r"""#!/bin/sh
case "$*" in
    'compose version --short') echo 'v2.24.0'; exit 0 ;;
    'compose version') exit 0 ;;
esac
printf '%s\n' "$GA_APP_IMAGE"
"""
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "./install.sh", "status"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected_status
    if expected_status:
        assert result.stdout == ""
        assert "release.env must contain one digest-pinned GA_APP_IMAGE." in result.stderr
    else:
        assert result.stdout == f"{IMAGE}\n"
