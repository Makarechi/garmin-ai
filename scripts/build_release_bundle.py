"""Build a source-free, digest-pinned Docker Compose installation candidate."""

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

from garmin_ai.operations import REVISION

ROOT = Path(__file__).resolve().parents[1]
APP_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
REVISION_LABEL = "org.opencontainers.image.revision"
NATIVE_ENTRYPOINTS = (["garmin-ai-rust"], ["/usr/local/bin/garmin-ai-rust"])
LOCAL_IMAGE = "${COMPOSE_PROJECT_NAME:-garmin-ai}:local"
RELEASE_IMAGE = "${GA_APP_IMAGE:?Set GA_APP_IMAGE to a digest-pinned application image}"
CONTENTS = (
    ".env.example",
    "LICENSE",
    "compose.release.yml",
    "install.sh",
    "scripts/configure.py",
    "scripts/release_mounts.py",
    "scripts/serve_demo.py",
    "scripts/verify_release_backup.py",
    "src/garmin_ai/static/dashboard/app.js",
    "src/garmin_ai/static/dashboard/index.html",
    "src/garmin_ai/static/dashboard/styles.css",
    "docs/release-install.md",
    "docs/provider-consent.md",
    "docs/operations.md",
    "docs/telegram-pairing.md",
    "docs/access-scopes.md",
    "docs/operational-acceptance.md",
    "docs/community-pilot-kit.md",
    "docs/implementation-backlog.md",
    "docs/test-coverage.md",
    "docs/universal-acceptance.md",
    "docs/verification.md",
    "docs/wearable-capability-spike.md",
    "tests/universal_acceptance_manifest.json",
)
NATIVE_CONTENTS = tuple(name for name in CONTENTS if not name.startswith(("scripts/", "src/"))) + (
    "compose.gcp.yml",
    "deploy/gcp/bootstrap.sh",
    "deploy/gcp/Caddyfile.garmin-auth",
    "deploy/gcp/garmin-ai-release-cleanup-native.service",
    "deploy/gcp/garmin-ai-release-cleanup.timer",
    "deploy/gcp/install-native-cleanup.sh",
    "docs/gcp-deployment.md",
    "docs/release-cleanup.md",
    "migration/rust-native-image.md",
    "migration/rust-native-schema.md",
)


def release_compose(source: str) -> str:
    build = f"    build: .\n    image: {LOCAL_IMAGE}\n"
    if source.count(build) != 1 or source.count(f"    image: {LOCAL_IMAGE}\n") != 3:
        raise ValueError("Development Compose service layout changed; review release rendering")
    result = source.replace(build, f"    image: {RELEASE_IMAGE}\n", 1)
    result = result.replace(f"    image: {LOCAL_IMAGE}\n", f"    image: {RELEASE_IMAGE}\n")
    if "build:" in result or LOCAL_IMAGE in result:
        raise ValueError("Release Compose must not build from local source")
    return result


def check_compose() -> None:
    expected = release_compose((ROOT / "compose.yml").read_text())
    actual = (ROOT / "compose.release.yml").read_text()
    if actual != expected:
        raise ValueError("compose.release.yml differs from the reviewed development layout")


def registry_document(image: str, *options: str) -> dict:
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", *options, image],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        if options == ("--raw",):
            expected = image.rsplit("@sha256:", 1)[1]
            if hashlib.sha256(result).hexdigest() != expected:
                raise ValueError("Registry manifest differs from the requested image digest")
        value = json.loads(result)
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Could not inspect the pinned image with Docker Buildx") from None
    if not isinstance(value, dict):
        raise ValueError("Registry returned an invalid image document")
    return value


def verify_native_image(image: str, revision: str) -> None:
    repository = image.split("@", 1)[0]
    pending = [image]
    verified = set()
    while pending:
        reference = pending.pop()
        if reference in verified:
            continue
        manifest = registry_document(reference, "--raw")
        if "manifests" in manifest:
            try:
                for descriptor in manifest["manifests"]:
                    platform = descriptor.get("platform") or {}
                    annotations = descriptor.get("annotations") or {}
                    if (
                        platform.get("os") == platform.get("architecture") == "unknown"
                        and annotations.get("vnd.docker.reference.type") == "attestation-manifest"
                    ):
                        continue
                    member = f"{repository}@{descriptor['digest']}"
                    if not APP_IMAGE_RE.fullmatch(member):
                        raise ValueError("Registry image index contains an invalid member digest")
                    pending.append(member)
            except (AttributeError, KeyError, TypeError):
                raise ValueError("Registry returned an invalid image index") from None
            continue
        config = registry_document(reference, "--format", "{{json .Image}}")
        try:
            settings = config["config"]
            label = (settings.get("Labels") or {}).get(REVISION_LABEL)
            entrypoint = settings.get("Entrypoint")
        except (AttributeError, KeyError, TypeError):
            raise ValueError("Registry returned an invalid image configuration") from None
        if config.get("os") != "linux" or entrypoint not in NATIVE_ENTRYPOINTS:
            raise ValueError("The pinned image must start the native Garmin AI application")
        if label != revision:
            raise ValueError("The pinned native image revision does not match the bundle checkout")
        verified.add(reference)
    if not verified:
        raise ValueError("The pinned image index contains no native application images")


def build_bundle(
    app_image: str, *, sha: str, output: Path, runtime: str = "python"
) -> tuple[Path, Path]:
    if not APP_IMAGE_RE.fullmatch(app_image):
        raise ValueError("Application image must include a full sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Git revision must be a complete commit SHA")
    if runtime not in ("python", "rust"):
        raise ValueError("Unsupported application runtime")
    native = runtime == "rust"
    if native:
        verify_native_image(app_image, sha)
    else:
        check_compose()
    compose_source = ROOT / ("compose.rust-release.yml" if native else "compose.release.yml")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    database_image = re.search(
        r"^    image: (timescale/timescaledb:[^\s]+@sha256:[0-9a-f]{64})$",
        compose_source.read_text(),
        re.MULTILINE,
    )
    if database_image is None:
        raise ValueError("Database image must be pinned by digest")
    manifest = {
        "format": "garmin-ai-install-candidate-v1",
        "version": version,
        "git_sha": sha,
        "app_image": app_image,
        "database_image": database_image.group(1),
        "database_revision": REVISION,
        "validated_platforms": [],
        "release_status": "candidate; operational and license acceptance pending",
    }
    if native:
        manifest["runtime"] = "rust"
    native_suffix = "-native" if native else ""
    prefix = f"garmin-ai-{version}{native_suffix}-{sha[:8]}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{prefix}.tar.gz"
    checksum = output / f"{archive.name}.sha256"
    with TemporaryDirectory(prefix=".release-", dir=output) as temporary:
        staging = Path(temporary)
        staged_archive = staging / archive.name
        staged_checksum = staging / checksum.name
        with staged_archive.open("wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                for name in NATIVE_CONTENTS if native else CONTENTS:
                    source = compose_source if name == "compose.release.yml" else ROOT / name
                    if native and name == "compose.gcp.yml":
                        source = ROOT / "compose.rust-gcp.yml"
                    data = source.read_bytes()
                    executable = name in {
                        "install.sh",
                        "deploy/gcp/bootstrap.sh",
                        "deploy/gcp/install-native-cleanup.sh",
                    }
                    _add(tar, f"{prefix}/{name}", data, 0o755 if executable else 0o644)
                runtime_setting = "GA_APP_RUNTIME=rust\n" if native else ""
                _add(
                    tar,
                    f"{prefix}/release.env",
                    f"GA_APP_IMAGE={app_image}\n{runtime_setting}".encode(),
                    0o644,
                )
                _add(
                    tar,
                    f"{prefix}/release-manifest.json",
                    (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
                    0o644,
                )
            raw.flush()
            os.fsync(raw.fileno())
        digest = hashlib.sha256(staged_archive.read_bytes()).hexdigest()
        with staged_checksum.open("w") as stream:
            stream.write(f"{digest}  {archive.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        _publish(staged_archive, staged_checksum, archive, checksum)
    return archive, checksum


def _publish(staged_archive: Path, staged_checksum: Path, archive: Path, checksum: Path) -> None:
    # Lock the output directory so concurrent builds publish one complete pair at a time.
    directory = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        previous = staged_archive.parent / ".previous-archive"
        try:
            os.link(archive, previous, follow_symlinks=False)
        except FileNotFoundError:
            had_previous = False
        else:
            had_previous = True
        staged_archive.replace(archive)
        try:
            staged_checksum.replace(checksum)
        except OSError:
            if had_previous:
                previous.replace(archive)
            else:
                archive.unlink()
            raise
    finally:
        os.close(directory)


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    item = tarfile.TarInfo(name)
    item.size = len(data)
    item.mode = mode
    item.mtime = 0
    item.uid = item.gid = 0
    tar.addfile(item, io.BytesIO(data))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-compose", action="store_true")
    parser.add_argument("--app-image")
    parser.add_argument("--runtime", choices=("python", "rust"), default="python")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.check_compose:
        check_compose()
        return
    if not args.app_image:
        parser.error("--app-image is required")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    if status:
        raise SystemExit("Commit or discard local changes before building a release candidate")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        archive, checksum = build_bundle(
            args.app_image, sha=sha, output=args.output, runtime=args.runtime
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(f"Created {archive} and {checksum}")


if __name__ == "__main__":
    main()
