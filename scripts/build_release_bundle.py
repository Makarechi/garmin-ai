"""Build a source-free, digest-pinned Docker Compose installation candidate."""

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path

from garmin_ai.operations import REVISION

ROOT = Path(__file__).resolve().parents[1]
APP_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
LOCAL_IMAGE = "${COMPOSE_PROJECT_NAME:-garmin-ai}:local"
RELEASE_IMAGE = "${GA_APP_IMAGE:?Set GA_APP_IMAGE to a digest-pinned application image}"
CONTENTS = (
    ".env.example",
    "LICENSE",
    "compose.release.yml",
    "install.sh",
    "scripts/configure.py",
    "scripts/serve_demo.py",
    "src/garmin_ai/static/dashboard/app.js",
    "src/garmin_ai/static/dashboard/index.html",
    "src/garmin_ai/static/dashboard/styles.css",
    "docs/release-install.md",
    "docs/provider-consent.md",
    "docs/operations.md",
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


def build_bundle(app_image: str, *, sha: str, output: Path) -> tuple[Path, Path]:
    if not APP_IMAGE_RE.fullmatch(app_image):
        raise ValueError("Application image must include a full sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Git revision must be a complete commit SHA")
    check_compose()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    database_image = re.search(
        r"^    image: (timescale/timescaledb:[^\s]+@sha256:[0-9a-f]{64})$",
        (ROOT / "compose.release.yml").read_text(),
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
    prefix = f"garmin-ai-{version}-{sha[:8]}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{prefix}.tar.gz"
    with archive.open("wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            for name in CONTENTS:
                data = (ROOT / name).read_bytes()
                _add(tar, f"{prefix}/{name}", data, 0o755 if name in {"install.sh"} else 0o644)
            _add(tar, f"{prefix}/release.env", f"GA_APP_IMAGE={app_image}\n".encode(), 0o644)
            _add(
                tar,
                f"{prefix}/release-manifest.json",
                (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
                0o644,
            )
    checksum = output / f"{archive.name}.sha256"
    checksum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    return archive, checksum


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
    archive, checksum = build_bundle(args.app_image, sha=sha, output=args.output)
    print(f"Created {archive} and {checksum}")


if __name__ == "__main__":
    main()
