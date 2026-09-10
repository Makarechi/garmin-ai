"""Conservative backup staging estimates; never return source paths or contents."""

import os
import shutil
from pathlib import Path

from sqlalchemy import text

from garmin_ai.archive import has_path_redirect

RESERVE = 256 * 1024 * 1024


class BackupSpaceInsufficient(RuntimeError):
    pass


def existing_directory(path):
    path = Path(path)
    if has_path_redirect(path):
        raise ValueError("Backup storage must not contain symlinks, junctions or redirected paths")
    while not path.exists():
        path = path.parent
    if not path.is_dir():
        raise ValueError("Backup storage must be a directory")
    return path


def source_estimate(settings):
    size, files = 0, 0
    roots = [settings.data_dir / "raw", settings.token_dir]
    manifest = settings.data_dir / "coverage-report.json"
    if has_path_redirect(manifest):
        raise ValueError("Backup source contains redirected paths")
    if manifest.is_file():
        size, files = manifest.stat().st_size, 1
    for root in roots:
        if has_path_redirect(root):
            raise ValueError("Backup source contains redirected paths")
        pending = [root] if root.exists() else []
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink() or path.is_junction():
                        raise ValueError("Backup source contains redirected paths")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        size += entry.stat(follow_symlinks=False).st_size
                        files += 1
    # tar headers, padding and long-name metadata; estimates are not reservations.
    return size + (files + 1) * 4096 + 10240


def backup_space(engine, settings, destination, *, export_bytes=None, export_staged=False):
    local = existing_directory(settings.data_dir / "backup-work")
    remote = existing_directory(Path(destination).parent)
    if export_bytes is None:
        with engine.connect() as connection:
            database_bytes = int(
                connection.scalar(text("SELECT pg_database_size(current_database())"))
            )
        # JSON/base64 can expand physical storage; leave explicit conservative headroom.
        export_bytes = max(1024 * 1024, database_bytes * 8)
    archive_bytes = int(export_bytes) + source_estimate(settings)
    local_required = (0 if export_staged else int(export_bytes)) + archive_bytes
    remote_required = archive_bytes + 64
    same_volume = local.stat().st_dev == remote.stat().st_dev
    if same_volume:
        volumes = [
            {
                "role": "shared",
                "free_bytes": shutil.disk_usage(local).free,
                "required_bytes": local_required + remote_required + RESERVE,
            }
        ]
    else:
        volumes = [
            {
                "role": "staging",
                "free_bytes": shutil.disk_usage(local).free,
                "required_bytes": local_required + RESERVE,
            },
            {
                "role": "destination",
                "free_bytes": shutil.disk_usage(remote).free,
                "required_bytes": remote_required + RESERVE,
            },
        ]
    return {
        "status": "ready"
        if all(v["free_bytes"] >= v["required_bytes"] for v in volumes)
        else "insufficient",
        "estimate_only": True,
        "reserve_bytes": RESERVE,
        "volumes": volumes,
    }


def require_backup_space(engine, settings, destination, *, export_bytes=None, export_staged=False):
    report = backup_space(
        engine, settings, destination, export_bytes=export_bytes, export_staged=export_staged
    )
    if report["status"] != "ready":
        raise BackupSpaceInsufficient(
            "Insufficient free space for backup staging and encryption; run backup-space"
        )
    return report
