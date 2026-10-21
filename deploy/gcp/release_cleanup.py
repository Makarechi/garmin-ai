#!/usr/bin/env python3
"""Bounded, host-side release retention. Requires only Debian's Python standard library."""

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import tarfile
import time
from pathlib import Path

STATE_DIR = Path("/var/lib/garmin-ai-release-cleanup")
LOCK = Path("/run/lock/garmin-ai-release-cleanup.lock")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
ARCHIVE_NAME = re.compile(r"garmin-ai-[a-zA-Z0-9_.-]+\.tar\.gz\Z")
CONTAINER_FORMAT = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"status":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},'
    '"health":{{if index .State "Health"}}{{json .State.Health.Status}}{{else}}null{{end}}}'
)


def docker(*args):
    # Never inspect/print container environment variables, which contain secrets.
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=600, check=False
    )
    if result.returncode:
        raise RuntimeError("Docker command failed: " + " ".join(args[:2]))
    return result.stdout


def containers(project=None):
    args = ["container", "ls", "-aq"]
    if project:
        args += ["--filter", f"label=com.docker.compose.project={project}"]
    ids = docker(*args).split()
    if not ids:
        return []
    return [
        json.loads(line)
        for line in docker("container", "inspect", "--format", CONTAINER_FORMAT, *ids).splitlines()
    ]


def current_image(*, require_healthy=True):
    found = containers("garmin-ai")
    apps = [item for item in found if item["service"] in {"api", "worker"}]
    if (
        len(apps) != 2
        or {item["service"] for item in apps} != {"api", "worker"}
        or any(item["status"] != "running" for item in apps)
        or (require_healthy and any(item["health"] != "healthy" for item in apps))
        or len({item["image"] for item in apps}) != 1
        or any(item["service"] == "migrate" and item["status"] != "exited" for item in found)
    ):
        raise RuntimeError(
            "Cleanup requires healthy api and worker on the same image, with no migration"
        )
    return apps[0]["image"], found


def image_references():
    rows = docker(
        "image",
        "ls",
        "--no-trunc",
        "--format",
        '{"id":{{json .ID}},"repository":{{json .Repository}},"tag":{{json .Tag}}}',
    )
    images = {}
    for line in rows.splitlines():
        item = json.loads(line)
        images.setdefault(item["id"], []).append(item)
    return images


def retention(state, current, images, rollback=None, archive_dirs=None):
    if not IMAGE_ID.fullmatch(current):
        raise RuntimeError("Invalid current image identity")
    if state is None:
        if not rollback:
            raise RuntimeError("Initialize with an explicitly verified rollback image first")
        previous = docker("image", "inspect", "--format", "{{.Id}}", rollback).strip()
        directories = archive_dirs or ["/opt/garmin-ai/uploads"]
    else:
        if rollback:
            raise RuntimeError("Rollback initialization is only allowed with no existing state")
        if state.get("version") != 1:
            raise RuntimeError("Unsupported retention state")
        previous = state["rollback"] if current == state["current"] else state["current"]
        directories = archive_dirs if archive_dirs is not None else state["archive_dirs"]
    for identity in (current, previous):
        if (
            not IMAGE_ID.fullmatch(identity)
            or identity not in images
            or not any(row["repository"] == "garmin-ai" for row in images[identity])
        ):
            raise RuntimeError("Both retained application images must exist locally")
    if previous == current:
        raise RuntimeError("A distinct, verified rollback image is required")
    for directory in directories:
        path = Path(directory)
        if (
            not path.is_absolute()
            or path.resolve() != path
            or any(part.lower() in {"backup", "backups", "data", "tokens"} for part in path.parts)
        ):
            raise RuntimeError(
                "Archive directories must be canonical upload paths outside private data"
            )
    return {"version": 1, "current": current, "rollback": previous, "archive_dirs": directories}


def image_archive(stream):
    """Recognize Docker save exports without extracting anything or trusting names alone."""
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                if member.name != "manifest.json":
                    continue
                if not member.isfile() or member.size > 1024 * 1024:
                    return False
                manifest = json.load(archive.extractfile(member))
                return (
                    bool(manifest)
                    and isinstance(manifest, list)
                    and all(
                        isinstance(item, dict)
                        and isinstance(item.get("Config"), str)
                        and isinstance(item.get("Layers"), list)
                        and isinstance(item.get("RepoTags"), list)
                        and bool(item["RepoTags"])
                        and all(
                            isinstance(tag, str) and tag.startswith("garmin-ai:")
                            for tag in item["RepoTags"]
                        )
                        for item in manifest
                    )
                )
    except (tarfile.TarError, ValueError, OSError, EOFError):
        return False
    return False


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def archive_candidates(directory, now):
    # No recursion: backup/data directories and old release configurations are never visited.
    root = Path(directory)
    if not root.exists():
        return
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if not ARCHIVE_NAME.fullmatch(entry.name):
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or now - info.st_mtime < 86400:
                continue
            with open(
                entry.path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)
            ) as stream:
                if fingerprint(os.fstat(stream.fileno())) != fingerprint(info):
                    continue
                if image_archive(stream):
                    yield Path(entry.path), info


def remove_archive(path, expected):
    # Fail closed if an upload replaces the file between inspection and deletion.
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if fingerprint(actual) != fingerprint(expected) or actual.st_nlink != 1:
            raise RuntimeError("Archive changed during cleanup")
        os.unlink(path.name, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW, 0o600)) as stream:
        json.dump(state, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def cleanup(state, *, apply=False, record_only=False):
    # Existing verified pins remain protected even when a busy host misses a probe.
    # Health is required when recording a new deployment, not to reclaim old artifacts.
    current, project_containers = current_image(require_healthy=False)
    if current != state["current"]:
        raise RuntimeError("Deployment changed during cleanup")
    images = image_references()
    keep = {state["current"], state["rollback"]}
    if not keep <= images.keys():
        raise RuntimeError("A retained image disappeared; refusing cleanup")
    obsolete = {
        identity: [row["repository"] + ":" + row["tag"] for row in rows]
        for identity, rows in images.items()
        if identity not in keep
        and all(row["repository"] == "garmin-ai" and row["tag"] != "<none>" for row in rows)
    }
    completed = [
        item
        for item in project_containers
        if item["service"] == "migrate"
        and item["status"] == "exited"
        and item["exit_code"] == 0
        and item["image"] in obsolete
    ]
    protected = {
        item["image"]
        for item in containers()
        if item["id"] not in {migration["id"] for migration in completed}
    }
    obsolete = {identity: refs for identity, refs in obsolete.items() if identity not in protected}
    report = {
        "apply": apply,
        "current": current,
        "rollback": state["rollback"],
        "images": list(obsolete.values()),
        "archives": [],
        "archive_bytes": 0,
    }
    if record_only:
        return report

    def unchanged():
        if current_image(require_healthy=False)[0] != current:
            raise RuntimeError("Deployment changed during cleanup")
        if not keep <= image_references().keys():
            raise RuntimeError("A retained image disappeared; refusing cleanup")

    if apply:
        for migration in completed:
            unchanged()
            # No -v: removing a successful one-shot container never removes its volumes.
            docker("container", "rm", migration["id"])
        for identity, references in obsolete.items():
            unchanged()
            # Recheck tag identity; never remove a replacement uploaded after planning.
            for reference in references:
                if docker("image", "inspect", "--format", "{{.Id}}", reference).strip() != identity:
                    raise RuntimeError("Image tag changed during cleanup")
            docker("image", "rm", *references)  # Never --force or any global prune.
    for directory in state["archive_dirs"]:
        for path, info in archive_candidates(directory, time.time()):
            report["archives"].append(str(path))
            report["archive_bytes"] += info.st_size
            if apply:
                unchanged()
                remove_archive(path, info)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="default is a read-only preview")
    parser.add_argument("--record-only", action="store_true")
    parser.add_argument("--initialize-rollback")
    parser.add_argument("--archive-dir", action="append")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--lock-file", type=Path, default=LOCK)
    args = parser.parse_args(argv)
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with open(args.lock_file, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"skipped": "deployment or cleanup lock is held"}))
                return 0
            path = args.state_dir / "state.json"
            old = json.loads(path.read_text()) if path.exists() else None
            current, _ = current_image(require_healthy=False)
            if old is None or current != old.get("current"):
                if current_image()[0] != current:
                    raise RuntimeError("Deployment changed while verifying the new release")
            state = retention(
                old, current, image_references(), args.initialize_rollback, args.archive_dir
            )
            if args.apply and state != old:
                save_state(path, state)
            print(json.dumps(cleanup(state, apply=args.apply, record_only=args.record_only)))
        return 0
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        # No raw Docker output or configuration contents in the journal.
        print(
            json.dumps(
                {"error": str(error) if isinstance(error, RuntimeError) else type(error).__name__}
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
