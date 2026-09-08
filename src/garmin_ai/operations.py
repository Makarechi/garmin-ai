"""Portable exports and authenticated streaming backups; no secrets in logs."""

import base64
import gzip
import json
import os
import shutil
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import Date, DateTime, Uuid, func, insert, select, text

from garmin_ai.archive import atomic_private_write, fsync_directory, private_directory
from garmin_ai.models import Base

MAGIC = b"GARMINAI1"
REVISION = "4c9e28f110ab"
COMPATIBLE_EXPORT_REVISIONS = {"bfccd06bf1c6", REVISION}
CHUNK = 1024 * 1024


def ensure_parent(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("Destination parent must be a directory")


def backup_key(settings):
    key = base64.urlsafe_b64decode(settings.backup_key.get_secret_value())
    if len(key) != 32:
        raise ValueError("GA_BACKUP_KEY must encode 32 random bytes")
    return key


@contextmanager
def export_snapshot(engine):
    # Acquire the session lock before the repeatable-read snapshot is established.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_advisory_lock_shared(72104622)"))
        conn.rollback()
        try:
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                yield conn
        finally:
            conn.rollback()
            conn.execution_options(isolation_level="AUTOCOMMIT", stream_results=False).execute(
                text("SELECT pg_advisory_unlock_shared(72104622)")
            )


def export_database(engine, destination: Path):
    ensure_parent(destination.parent)
    counts = {}
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        tmp = Path(temporary.name)
    try:
        with (
            export_snapshot(engine) as conn,
            gzip.open(tmp, "wt", encoding="utf-8") as output,
        ):
            revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != REVISION:
                raise ValueError("Unexpected database schema")
            if conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM app_state WHERE key='maintenance:erased')")
            ):
                raise ValueError("Cannot export erased storage; explicitly resume storage first")
            output.write(
                json.dumps(
                    {
                        "format": "garmin-ai-jsonl-v1",
                        "revision": revision,
                        "exported_at": datetime.now(UTC).isoformat(),
                    }
                )
                + "\n"
            )
            for table in Base.metadata.sorted_tables:
                count = 0
                result = conn.execution_options(stream_results=True).execute(select(table))
                for row in result.mappings():
                    output.write(
                        json.dumps(
                            {"table": table.name, "row": dict(row)},
                            default=lambda x: (
                                x.isoformat() if isinstance(x, (date, datetime)) else str(x)
                            ),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    count += 1
                counts[table.name] = count
            output.write(json.dumps({"counts": counts}) + "\n")
        with tmp.open("r+b") as completed:
            os.fsync(completed.fileno())
        os.replace(tmp, destination)
        fsync_directory(destination.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return counts


def restore_database(engine, source: Path):
    """Restore only into an empty migrated database; one transaction or no changes."""
    tables = Base.metadata.tables
    counts = {name: 0 for name in tables}
    with engine.begin() as conn, gzip.open(source, "rt", encoding="utf-8") as stream:
        conn.execute(text("SELECT pg_advisory_xact_lock(72104622)"))
        header = json.loads(next(stream))
        if (
            header.get("format") != "garmin-ai-jsonl-v1"
            or header.get("revision") not in COMPATIBLE_EXPORT_REVISIONS
            or conn.scalar(text("SELECT version_num FROM alembic_version")) != REVISION
        ):
            raise ValueError("Incompatible export or destination schema")
        for table in tables.values():
            query = select(func.count()).select_from(table)
            if table.name == "app_state":
                query = query.where(table.c.key != "maintenance:erased")
            if conn.scalar(query):
                raise ValueError("Restore requires an empty destination database")
        conn.execute(text("DELETE FROM app_state WHERE key='maintenance:erased'"))
        footer = None
        batch = []
        batch_table = None

        def flush():
            if batch:
                conn.execute(insert(batch_table), batch)
                batch.clear()

        for line in stream:
            record = json.loads(line)
            if "counts" in record:
                footer = record["counts"]
                if stream.read().strip():
                    raise ValueError("Unexpected data after export footer")
                break
            table = tables[record["table"]]
            if batch_table is not table:
                flush()
                batch_table = table
            values = record["row"]
            if table.name == "app_state" and values.get("key") == "maintenance:erased":
                raise ValueError("Export contains erased storage state")
            for name, value in values.items():
                if value is None:
                    continue
                kind = table.c[name].type
                if isinstance(kind, DateTime):
                    values[name] = datetime.fromisoformat(value)
                elif isinstance(kind, Date):
                    values[name] = date.fromisoformat(value)
                elif isinstance(kind, Uuid):
                    values[name] = UUID(value)
            batch.append(values)
            if len(batch) >= 1000:
                flush()
            counts[table.name] += 1
        flush()
        if footer != counts:
            raise ValueError("Incomplete export")
        # Explicit IDs from the snapshot must not collide with subsequent inserts.
        for table in tables.values():
            for column in table.primary_key.columns:
                if column.autoincrement is True or (
                    column.autoincrement == "auto"
                    and isinstance(column.type.python_type, type)
                    and column.type.python_type is int
                    and len(table.primary_key.columns) == 1
                ):
                    sequence = conn.scalar(
                        text("SELECT pg_get_serial_sequence(:table, :column)"),
                        {"table": table.name, "column": column.name},
                    )
                    if sequence:
                        maximum = conn.scalar(select(func.max(column)))
                        conn.execute(
                            text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                            {
                                "sequence": sequence,
                                "value": maximum or 1,
                                "called": maximum is not None,
                            },
                        )
    return counts


def encrypt_file(source: Path, destination: Path, key: bytes):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination already exists")
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    ensure_parent(destination.parent)
    fd, name = tempfile.mkstemp(dir=destination.parent)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            dst.write(MAGIC + nonce)
            while block := src.read(CHUNK):
                dst.write(encryptor.update(block))
            dst.write(encryptor.finalize())
            dst.write(encryptor.tag)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(name, destination)
        fsync_directory(destination.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def decrypt_file(source: Path, destination: Path, key: bytes):
    if destination.exists():
        raise ValueError("Decryption destination already exists")
    size = source.stat().st_size
    if size < len(MAGIC) + 12 + 16:
        raise ValueError("Invalid backup")
    try:
        with source.open("rb") as src:
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError("Unsupported backup")
            nonce = src.read(12)
            src.seek(-16, 2)
            tag = src.read(16)
            src.seek(len(MAGIC) + 12)
            remaining = size - len(MAGIC) - 12 - 16
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            with destination.open("xb") as dst:
                destination.chmod(0o600)
                while remaining:
                    block = src.read(min(CHUNK, remaining))
                    if not block:
                        raise ValueError("Encrypted backup ended unexpectedly")
                    dst.write(decryptor.update(block))
                    remaining -= len(block)
                dst.write(decryptor.finalize())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def create_backup(engine, settings, destination: Path):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination already exists")
    for source in (settings.data_dir, settings.data_dir / "raw", settings.token_dir):
        if source.is_symlink():
            raise ValueError("Backup source root is a symlink")
    key = backup_key(settings)
    ensure_parent(destination.parent)
    for source in (settings.data_dir, settings.token_dir):
        if destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("Backup destination must be outside archived source trees")
    # Plaintext staging stays beside the original local data, never on backup media.
    staging = private_directory(settings.data_dir / "backup-work")
    with tempfile.TemporaryDirectory(dir=staging) as work:
        root = Path(work)
        counts = export_database(engine, root / "database.jsonl.gz")
        with tarfile.open(root / "backup.tar", "w") as archive:
            archive.add(root / "database.jsonl.gz", arcname="database.jsonl.gz")
            manifest = settings.data_dir / "coverage-report.json"
            if manifest.is_file() and not manifest.is_symlink():
                archive.add(manifest, arcname="coverage-report.json", recursive=False)
            for directory, prefix in [
                (settings.data_dir / "raw", "raw"),
                (settings.token_dir, "tokens"),
            ]:
                if directory.exists():
                    for path in sorted(directory.rglob("*")):
                        if path.is_symlink():
                            raise ValueError("Backup source contains a symlink")
                        if path.is_file():
                            archive.add(
                                path,
                                arcname=str(Path(prefix) / path.relative_to(directory)),
                                recursive=False,
                            )
        encrypt_file(root / "backup.tar", destination, key)
    return counts


def unpack_backup(settings, source: Path, destination: Path):
    """Verify authentication before unpacking; never overwrites an existing directory."""
    for protected in (settings.data_dir, settings.token_dir):
        if destination.resolve().is_relative_to(
            protected.resolve()
        ) or protected.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Unpack into a separate recovery directory outside protected storage")
    if destination.exists():
        raise ValueError("Unpack destination already exists")
    ensure_parent(destination.parent)
    with tempfile.TemporaryDirectory(dir=destination.parent) as work:
        root = Path(work)
        decrypt_file(source, root / "backup.tar", backup_key(settings))
        extracted = private_directory(root / "unpacked")
        with tarfile.open(root / "backup.tar") as archive:
            for member in archive:
                relative = Path(member.name)
                if (
                    not member.isfile()
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or (
                        relative.parts[0] not in {"raw", "tokens"}
                        and member.name not in {"database.jsonl.gz", "coverage-report.json"}
                    )
                ):
                    raise ValueError("Unsafe backup member")
                target = extracted / relative
                private_directory(target.parent)
                with archive.extractfile(member) as src, target.open("xb") as dst:
                    target.chmod(0o600)
                    shutil.copyfileobj(src, dst)
        os.replace(extracted, destination)


def erase_all(engine, settings, confirmation: str):
    from garmin_ai.storage_files import exclusive_files

    with exclusive_files(settings, allow_erased=True):
        return _erase_all(engine, settings, confirmation)


def _erase_all(engine, settings, confirmation: str):
    if confirmation != "ERASE ALL LOCAL HEALTH DATA":
        raise ValueError("Exact erasure confirmation required")
    for path in (settings.data_dir, settings.token_dir):
        if (
            path.is_symlink()
            or path.resolve() == Path.home()
            or len(path.resolve().parts) < 4
            or path.resolve() == Path.cwd()
            or path.resolve() in Path.cwd().parents
        ):
            raise ValueError("Unsafe erasure directory")
    # Require stopped workers; the lock is session-scoped until deletion completes.
    with engine.connect() as conn:
        if not conn.scalar(text("SELECT pg_try_advisory_lock(72104620)")):
            raise ValueError("Stop the runtime before erasing data")
        conn.commit()
        try:
            with conn.begin():
                conn.execute(text("SELECT pg_advisory_xact_lock(72104622)"))
                names = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
                conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
                conn.execute(
                    text(
                        "INSERT INTO app_state (key, value) VALUES ('maintenance:erased', '{\"disabled\": true}'::jsonb)"
                    )
                )
            atomic_private_write(
                settings.lock_dir / "erased",
                b"Storage explicitly erased.\n",
                preserve_parent_mode=True,
            )
            for path in (settings.data_dir, settings.token_dir):
                if path.exists():
                    if (
                        path.is_symlink()
                        or path.resolve() == Path.home()
                        or len(path.resolve().parts) < 4
                    ):
                        raise ValueError("Unsafe erasure directory")
                    shutil.rmtree(path)
                if path.parent.is_dir():
                    fsync_directory(path.parent)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(72104620)"))
    return {
        "erased": True,
        "note": "Separately stored backups and provider-side copies are not affected.",
    }


def prune_scheduled_backups(directory: Path, keep: int, *, preserve: Path | None = None):
    if keep < 1:
        raise ValueError("At least one backup must be retained")
    snapshots = []
    for path in directory.glob("garmin-ai-????-??-??.enc"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            date.fromisoformat(path.name.removeprefix("garmin-ai-").removesuffix(".enc"))
        except ValueError:
            continue
        snapshots.append(path)
    for path in sorted(snapshots, key=lambda p: (p == preserve, p.name), reverse=True)[keep:]:
        path.unlink()


def scheduled_backup(engine, settings, destination: Path):
    """Recover a completed snapshot after a worker crash without overwriting it."""
    existed = destination.exists()
    if existed:
        staging = private_directory(settings.data_dir / "backup-work")
        with tempfile.TemporaryDirectory(dir=staging) as work:
            decrypt_file(destination, Path(work) / "verified.tar", backup_key(settings))
    else:
        create_backup(engine, settings, destination)
    prune_scheduled_backups(destination.parent, settings.backup_keep_daily, preserve=destination)
    return datetime.fromtimestamp(destination.stat().st_mtime, UTC)
