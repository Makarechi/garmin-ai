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
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_LABEL = "org.opencontainers.image.revision"
NATIVE_ENTRYPOINTS = (["garmin-ai-rust"], ["/usr/local/bin/garmin-ai-rust"])
NATIVE_VERSION_PATTERNS = {
    "fit_profile_version": r"[0-9]+(?:\.[0-9]+)+",
    "database_export_format": r"garmin-ai-[a-z0-9]+-v[0-9]+",
    "backup_envelope": r"GARMINAI[0-9]+",
}
RELEASE_IMAGE = "${GA_APP_IMAGE:?Set GA_APP_IMAGE to a digest-pinned application image}"
CONTENTS = (
    ".env.example",
    "LICENSE",
    "compose.release.yml",
    "install.sh",
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
    "compose.gcp.yml",
    "deploy/gcp/bootstrap.sh",
    "deploy/gcp/Caddyfile.garmin-auth",
    "deploy/gcp/garmin-ai-release-cleanup-native.service",
    "deploy/gcp/garmin-ai-release-cleanup.timer",
    "deploy/gcp/install-native-cleanup.sh",
    "docs/gcp-deployment.md",
    "docs/release-cleanup.md",
    "migration/cutover-runbook.md",
    "migration/rollback-runbook.md",
    "migration/rust-native-image.md",
    "migration/rust-native-schema.md",
)


def check_compose(source: str) -> None:
    if f"image: {RELEASE_IMAGE}" not in source or re.search(r"^\s*build:", source, re.MULTILINE):
        raise ValueError("Release Compose must use the pinned application image without a build")


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


def verify_native_image(image: str, revision: str) -> dict[str, dict[str, str]]:
    repository = image.split("@", 1)[0]
    pending: list[tuple[str, tuple[str, str] | None]] = [(image, None)]
    seen = set()
    platform_images: dict[str, dict[str, str]] = {}
    while pending:
        item = pending.pop()
        if item in seen:
            continue
        seen.add(item)
        reference, expected_platform = item
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
                    declared_platform = (
                        (platform["os"], platform["architecture"]) if platform else None
                    )
                    if declared_platform and not all(
                        isinstance(part, str) for part in declared_platform
                    ):
                        raise ValueError("Registry returned an invalid image index platform")
                    if (
                        expected_platform is not None
                        and declared_platform is not None
                        and declared_platform != expected_platform
                    ):
                        raise ValueError("Native image platform does not match its index")
                    pending.append((member, declared_platform or expected_platform))
            except (AttributeError, KeyError, TypeError):
                raise ValueError("Registry returned an invalid image index") from None
            continue
        config = registry_document(reference, "--format", "{{json .Image}}")
        try:
            settings = config["config"]
            label = (settings.get("Labels") or {}).get(REVISION_LABEL)
            entrypoint = settings.get("Entrypoint")
            config_digest = manifest["config"]["digest"]
        except (AttributeError, KeyError, TypeError):
            raise ValueError("Registry returned an invalid image configuration") from None
        if config.get("os") != "linux" or entrypoint not in NATIVE_ENTRYPOINTS:
            raise ValueError("The pinned image must start the native Garmin AI application")
        if label != revision:
            raise ValueError("The pinned native image revision does not match the bundle checkout")
        architecture = config.get("architecture")
        if not isinstance(architecture, str) or not re.fullmatch(r"[a-z0-9_]+", architecture):
            raise ValueError("The pinned native image has an invalid architecture")
        if expected_platform is not None and expected_platform != ("linux", architecture):
            raise ValueError("Native image platform does not match its index")
        if not isinstance(config_digest, str) or not DIGEST_RE.fullmatch(config_digest):
            raise ValueError("The pinned native image has an invalid configuration digest")
        platform = f"linux/{architecture}"
        platform_image = {
            "manifest_digest": reference.rsplit("@", 1)[1],
            "config_digest": config_digest,
        }
        existing = platform_images.get(platform)
        if existing is not None and existing != platform_image:
            raise ValueError(
                f"The pinned native image index contains multiple images for {platform}"
            )
        platform_images[platform] = platform_image
    if not platform_images:
        raise ValueError("The pinned image index contains no native application images")
    return platform_images


def validate_release_info(info: object, manifest: dict) -> None:
    fixed = {
        "application_version": manifest["version"],
        "database_revision": manifest["database_revision"],
        "archive_json": "Python-compatible UTF-8 JSON",
    }
    parsers = ("garmin_parser_version", "fit_parser_version")
    expected = set(fixed) | set(parsers) | NATIVE_VERSION_PATTERNS.keys()
    if not (
        isinstance(info, dict)
        and info.keys() == expected
        and all(info[key] == value for key, value in fixed.items())
        and all(type(info[key]) is int and info[key] >= 0 for key in parsers)
        and all(
            isinstance(info[key], str) and re.fullmatch(pattern, info[key])
            for key, pattern in NATIVE_VERSION_PATTERNS.items()
        )
    ):
        raise ValueError("Native installation report has invalid release versions")


def attach_native_versions(manifest: dict, reports: tuple[Path, ...]) -> None:
    platform_images = manifest["app_platform_images"]
    version_info = {}
    hashes: dict[str, set[str]] = {}
    for path in reports:
        try:
            document = path.read_bytes()
            receipt = json.loads(document)
        except (OSError, ValueError):
            raise ValueError(f"Could not read native installation report {path.name}") from None
        if not isinstance(receipt, dict) or not isinstance(receipt.get("platform"), str):
            raise ValueError("Invalid native installation report")
        platform = "/".join(receipt["platform"].split("/")[:2])
        image = platform_images.get(platform)
        if (
            receipt.get("runtime") != "rust"
            or receipt.get("git_sha") != manifest["git_sha"]
            or receipt.get("app_image") != manifest["app_image"]
            or image is None
            or receipt.get("app_platform_image") != image
        ):
            raise ValueError("Native installation report does not match the release image")
        if not all(
            receipt.get(check) is True
            for check in (
                "native_runtime_processes_verified",
                "portable_database_export_verified",
                "native_backup_and_recovery_verified",
            )
        ):
            raise ValueError("Native installation report is missing storage verification")
        info = receipt.get("release_info")
        validate_release_info(info, manifest)
        if version_info and info != version_info:
            raise ValueError("Native installation reports disagree on release versions")
        version_info = info
        hashes.setdefault(platform, set()).add(hashlib.sha256(document).hexdigest())
    if hashes.keys() != platform_images.keys():
        raise ValueError("Native installation reports must cover every image platform")
    manifest["release_info"] = version_info
    manifest["native_installation_report_sha256"] = {
        platform: sorted(values) for platform, values in hashes.items()
    }


def build_bundle(
    app_image: str,
    *,
    sha: str,
    output: Path,
    native_installation_reports: tuple[Path, ...] = (),
) -> tuple[Path, Path]:
    if not APP_IMAGE_RE.fullmatch(app_image):
        raise ValueError("Application image must include a full sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Git revision must be a complete commit SHA")
    compose_source = ROOT / "compose.yml"
    compose = compose_source.read_text()
    check_compose(compose)
    version = tomllib.loads((ROOT / "crates/ga-app/Cargo.toml").read_text())["package"]["version"]
    revision = (ROOT / "crates/ga-db/schema-revision.txt").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{12}", revision):
        raise ValueError("Invalid Rust database revision")
    database_image = re.search(
        r"^    image: (timescale/timescaledb:[^\s]+@sha256:[0-9a-f]{64})$",
        compose,
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
        "database_revision": revision,
        "app_platform_images": verify_native_image(app_image, sha),
        "runtime": "rust",
        "validated_platforms": [],
        "release_status": "candidate; operational and license acceptance pending",
    }
    if native_installation_reports:
        attach_native_versions(manifest, native_installation_reports)
    prefix = f"garmin-ai-{version}-native-{sha[:8]}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{prefix}.tar.gz"
    checksum = output / f"{archive.name}.sha256"
    previous = archive.with_name(f".{archive.name}.previous")
    with (
        _recoverable_output(archive, checksum, previous) as directory,
        TemporaryDirectory(prefix=".release-", dir=output) as temporary,
    ):
        staging = Path(temporary)
        staged_archive = staging / archive.name
        staged_checksum = staging / checksum.name
        with staged_archive.open("wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                for name in CONTENTS:
                    source = compose_source if name == "compose.release.yml" else ROOT / name
                    data = source.read_bytes()
                    executable = name in {
                        "install.sh",
                        "deploy/gcp/bootstrap.sh",
                        "deploy/gcp/install-native-cleanup.sh",
                    }
                    _add(tar, f"{prefix}/{name}", data, 0o755 if executable else 0o644)
                _add(
                    tar,
                    f"{prefix}/release.env",
                    f"GA_APP_IMAGE={app_image}\nGA_APP_RUNTIME=rust\n".encode(),
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
        _publish(staged_archive, staged_checksum, archive, checksum, previous, directory)
    return archive, checksum


def _matches_checksum(path: Path, recorded: str, name: str) -> bool:
    try:
        with path.open("rb") as contents:
            digest = hashlib.file_digest(contents, "sha256").hexdigest()
    except FileNotFoundError:
        return False
    return recorded == f"{digest}  {name}\n"


@contextmanager
def _recoverable_output(archive: Path, checksum: Path, previous: Path):
    # Serialize recovery and publication with other builds using this output directory.
    directory = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        if previous.exists() or previous.is_symlink():
            if not checksum.exists():
                previous.replace(archive)
            else:
                recorded = checksum.read_text()
                if _matches_checksum(archive, recorded, archive.name):
                    previous.unlink()
                elif _matches_checksum(previous, recorded, archive.name):
                    previous.replace(archive)
                else:
                    raise ValueError(
                        f"Could not recover the interrupted publication of {archive.name}"
                    )
            os.fsync(directory)
        yield directory
    finally:
        os.close(directory)


def _publish(
    staged_archive: Path,
    staged_checksum: Path,
    archive: Path,
    checksum: Path,
    previous: Path,
    directory: int,
) -> None:
    try:
        os.link(archive, previous, follow_symlinks=False)
    except FileNotFoundError:
        had_previous = False
    else:
        had_previous = True
        os.fsync(directory)
    staged_archive.replace(archive)
    try:
        staged_checksum.replace(checksum)
    except OSError:
        if had_previous:
            previous.replace(archive)
        else:
            archive.unlink()
        os.fsync(directory)
        raise
    os.fsync(directory)
    if had_previous:
        previous.unlink()


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
    parser.add_argument("--runtime", choices=("rust",), help=argparse.SUPPRESS)
    parser.add_argument("--native-installation-report", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.check_compose:
        check_compose((ROOT / "compose.yml").read_text())
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
            args.app_image,
            sha=sha,
            output=args.output,
            native_installation_reports=tuple(args.native_installation_report),
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(f"Created {archive} and {checksum}")


if __name__ == "__main__":
    main()
