"""Portable exports and authenticated streaming backups; no secrets in logs."""

import base64
import ctypes
import errno
import gzip
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import Date, DateTime, Uuid, func, insert, select, text, update
from sqlalchemy.orm import Session

from garmin_ai.archive import (
    atomic_private_write,
    durable_directory,
    fsync_directory,
    has_path_redirect,
    private_directory,
)
from garmin_ai.models import Base

MAGIC = b"GARMINAI1"
REVISION = "c8f51d3a7e20"
COMPATIBLE_EXPORT_REVISIONS = {
    "bfccd06bf1c6",
    "4c9e28f110ab",
    "84a03c619f2e",
    "95b14d720a3f",
    "a637902bf114",
    "b91d02a4c703",
    "c42f8910e615",
    "d31e572abc90",
    "e6b8f0a13c72",
    "f18d7c0b42a1",
    "a94c7d2e610f",
    "c71a5e4d290b",
    "d02c6a7e31f4",
    "e13b7c8f42a0",
    "f103aa712b44",
    "b7c4e1a92d60",
    REVISION,
}
CHUNK = 1024 * 1024
NEUTRAL_MESSAGE_TABLES = (
    "conversations",
    "inbound_messages",
    "outbox_messages",
    "message_delivery_receipts",
)


def upgrade_legacy_messages(conn, counts):
    """Build neutral aliases after importing a pre-neutral portable export."""

    conn.execute(
        text(
            """
            INSERT INTO conversations (
                id, owner_id, channel, channel_instance_id, external_conversation_id,
                memory_epoch, state, share_owner_memory
            )
            SELECT
                md5('legacy:telegram:conversation:' || id::text)::uuid,
                id, 'telegram', 'primary',
                COALESCE(
                    (SELECT external_id FROM channel_bindings
                     WHERE owner_id = people.id AND channel = 'telegram'
                     ORDER BY confirmed_at LIMIT 1),
                    'legacy-owner'
                ),
                md5('legacy:telegram:epoch:' || id::text)::uuid,
                '{}'::jsonb, FALSE
            FROM people
            WHERE EXISTS (
                SELECT 1 FROM channel_bindings
                WHERE owner_id = people.id AND channel = 'telegram'
            ) OR EXISTS (
                SELECT 1 FROM telegram_updates
            ) OR EXISTS (
                SELECT 1 FROM app_state WHERE key LIKE 'outbox:update:%'
            )
            ON CONFLICT DO NOTHING
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO inbound_messages (
                id, owner_id, conversation_id, channel, channel_instance_id,
                external_event_id, external_message_id, sender_ref, occurred_at,
                received_at, kind, normalized_text, envelope, revision, status,
                operation_id, legacy_telegram_update_id
            )
            SELECT
                md5('legacy:telegram:update:' || updates.id::text)::uuid,
                people.id,
                md5('legacy:telegram:conversation:' || people.id::text)::uuid,
                'telegram', 'primary', updates.id::text,
                COALESCE(
                    updates.payload #>> '{message,message_id}',
                    updates.payload #>> '{callback_query,message,message_id}'
                ),
                COALESCE(
                    updates.payload #>> '{message,from,id}',
                    updates.payload #>> '{callback_query,from,id}',
                    'legacy-owner'
                ),
                CASE WHEN COALESCE(
                    updates.payload #>> '{message,date}',
                    updates.payload #>> '{callback_query,message,date}'
                ) ~ '^[0-9]+$' THEN to_timestamp(COALESCE(
                    updates.payload #>> '{message,date}',
                    updates.payload #>> '{callback_query,message,date}'
                )::double precision) ELSE NULL END,
                updates.received_at,
                CASE WHEN updates.payload ? 'callback_query' THEN 'action' ELSE 'text' END,
                COALESCE(
                    updates.payload #>> '{message,text}',
                    updates.payload #>> '{message,caption}'
                ),
                jsonb_build_object(
                    'legacy_telegram_update_id', updates.id,
                    'payload_retained_in', 'telegram_updates'
                ),
                1, updates.status,
                md5('legacy:telegram:operation:' || updates.id::text)::uuid,
                updates.id
            FROM telegram_updates AS updates CROSS JOIN people
            ON CONFLICT DO NOTHING
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO outbox_messages (
                id, owner_id, conversation_id, inbound_message_id, operation_id,
                intent, dedup_key, state, attempts, provider_reference, legacy_key,
                created_at, updated_at
            )
            SELECT
                md5('legacy:outbox:' || state.key)::uuid,
                people.id,
                md5('legacy:telegram:conversation:' || people.id::text)::uuid,
                CASE WHEN split_part(state.key, ':', 3) ~ '^[0-9]+$'
                          AND EXISTS (
                              SELECT 1 FROM telegram_updates
                              WHERE id = split_part(state.key, ':', 3)::bigint
                          )
                     THEN md5('legacy:telegram:update:' || split_part(state.key, ':', 3))::uuid
                     ELSE NULL END,
                CASE WHEN split_part(state.key, ':', 3) ~ '^[0-9]+$'
                     THEN md5('legacy:telegram:operation:' || split_part(state.key, ':', 3))::uuid
                     ELSE md5('legacy:outbox:operation:' || state.key)::uuid END,
                jsonb_build_object(
                    'legacy_key', state.key,
                    'text', state.value ->> 'text',
                    'keyboard', state.value -> 'keyboard'
                ),
                'legacy:' || state.key,
                CASE state.value ->> 'status'
                    WHEN 'pending' THEN 'queued'
                    WHEN 'sending' THEN 'uncertain'
                    WHEN 'sent' THEN 'provider_accepted'
                    WHEN 'uncertain' THEN 'uncertain'
                    WHEN 'failed' THEN 'failed'
                    WHEN 'cancelled' THEN 'cancelled'
                    ELSE 'uncertain'
                END,
                COALESCE((state.value ->> 'attempts')::integer, 0),
                state.value ->> 'message_id', state.key, state.updated_at, state.updated_at
            FROM app_state AS state CROSS JOIN people
            WHERE state.key LIKE 'outbox:update:%'
            ON CONFLICT DO NOTHING
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO message_delivery_receipts (
                id, outbox_message_id, state, observed_at, provider_reference
            )
            SELECT
                md5('legacy:receipt:' || legacy_key)::uuid,
                id, 'provider_accepted', updated_at, provider_reference
            FROM outbox_messages
            WHERE state = 'provider_accepted'
            ON CONFLICT DO NOTHING
            """
        )
    )
    for name in NEUTRAL_MESSAGE_TABLES:
        counts[name] = conn.scalar(text(f'SELECT count(*) FROM "{name}"'))


OWNER_TABLE_REVISIONS = {"e6b8f0a13c72", "f18d7c0b42a1", "a94c7d2e610f"}


def ensure_parent(path: Path):
    durable_directory(path)
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
    if destination.exists() or destination.is_symlink():
        raise ValueError("Export destination already exists")
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
        # Publish without replacing a destination created while the export was running.
        publish_file(tmp, destination)
        fsync_directory(destination.parent)
    finally:
        tmp.unlink(missing_ok=True)
        fsync_directory(tmp.parent)
    return counts


def restore_database(engine, source: Path, *, before_activate=None):
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
        bootstrap_people = 0
        bootstrap_definitions = 0
        bootstrap_metric_definitions = 0
        bootstrap_module_configs = 0
        for table in tables.values():
            query = select(func.count()).select_from(table)
            if table.name == "app_state":
                query = query.where(
                    table.c.key != "maintenance:erased",
                    ~table.c.key.startswith("bootstrap:"),
                )
            count = conn.scalar(query)
            if table.name == "people":
                bootstrap_people = count
                if count > 1:
                    raise ValueError("Restore requires an empty destination database")
                continue
            if table.name == "event_definitions":
                bootstrap_definitions = count
                custom = conn.scalar(
                    select(func.count()).select_from(table).where(table.c.namespace != "system")
                )
                if custom:
                    raise ValueError("Restore requires an empty destination database")
                continue
            if table.name == "event_definition_versions":
                continue
            if table.name == "metric_definitions":
                bootstrap_metric_definitions = count
                custom = conn.scalar(
                    select(func.count()).select_from(table).where(table.c.namespace != "system")
                )
                if custom:
                    raise ValueError("Restore requires an empty destination database")
                continue
            if table.name in {"metric_definition_versions", "event_metric_mappings"}:
                continue
            if table.name == "module_configs":
                bootstrap_module_configs = count
                continue
            if table.name == "conversations":
                generated = conn.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(
                        table.c.channel == "telegram",
                        table.c.channel_instance_id == "primary",
                        table.c.external_conversation_id == "legacy-owner",
                        table.c.state == {},
                    )
                )
                if count != generated or count > bootstrap_people:
                    raise ValueError("Restore requires an empty destination database")
                continue
            if count:
                raise ValueError("Restore requires an empty destination database")
        if bootstrap_definitions:
            conn.execute(tables["event_definitions"].delete())
        if bootstrap_metric_definitions:
            conn.execute(tables["metric_definitions"].delete())
        if bootstrap_people:
            conn.execute(tables["people"].delete())
        if bootstrap_module_configs:
            conn.execute(tables["module_configs"].delete())
        conn.execute(
            text("DELETE FROM app_state WHERE key='maintenance:erased' OR key LIKE 'bootstrap:%'")
        )
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
            if table.name == "events" and "topology" not in values:
                if values.get("end") is None and values.get("kind") in {"migraine", "illness"}:
                    values["topology"] = "open_interval"
                elif values.get("end") is None or values.get("end") == values.get("start"):
                    values["topology"] = "point"
                else:
                    values["topology"] = "bounded_interval"
            if table.name == "events" and "envelope_version" not in values:
                from garmin_ai.canonical_events import provenance_values

                canonical = provenance_values(
                    values["source"],
                    values["status"],
                    topology=values["topology"],
                )
                canonical["recorded_at"] = values["created_at"]
                canonical["ingested_at"] = values["created_at"]
                values.update(canonical)
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
        if header["revision"] != REVISION:
            person_id = conn.scalar(select(tables["people"].c.id).limit(1))
            if person_id is None:
                person_id = uuid4()
                conn.execute(
                    insert(tables["people"]),
                    {
                        "id": person_id,
                        "singleton": True,
                        "locale": "ru",
                        "timezone": "Europe/Bratislava",
                        "units": "metric",
                    },
                )
                counts["people"] = 1
            legacy_account = conn.scalar(
                select(tables["app_state"].c.value).where(
                    tables["app_state"].c.key == "account:garmin"
                )
            )
            if (
                counts["source_connections"] == 0
                and legacy_account
                and legacy_account.get("fingerprint")
            ):
                conn.execute(
                    insert(tables["source_connections"]),
                    {
                        "id": uuid4(),
                        "owner_id": person_id,
                        "provider": "garmin",
                        "namespace": "socialProfile.profileId:v1",
                        "external_id": str(legacy_account["fingerprint"]),
                        "confirmation_method": "legacy_account_binding",
                        "details": {
                            key: legacy_account[key]
                            for key in ("instance_id", "identity_contract")
                            if legacy_account.get(key) is not None
                        },
                    },
                )
                counts["source_connections"] = 1
            goals = conn.scalar(
                select(tables["app_state"].c.value).where(
                    tables["app_state"].c.key == "preferences:personal-goals"
                )
            )
            if goals is not None:
                conn.execute(
                    update(tables["app_state"])
                    .where(tables["app_state"].c.key == "preferences:personal-goals")
                    .values(value={**goals, "owner_id": str(person_id)})
                )
            if isinstance(footer, dict) and header["revision"] not in OWNER_TABLE_REVISIONS:
                for name in ("people", "source_connections", "channel_bindings"):
                    footer[name] = counts[name]
        registry_was_exported = isinstance(footer, dict) and "event_definitions" in footer
        if header["revision"] != REVISION and not registry_was_exported:
            registry = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                from garmin_ai.definitions import ensure_system_definitions

                ensure_system_definitions(registry, backfill=True)
                registry.commit()
            finally:
                registry.close()
            for name in ("event_definitions", "event_definition_versions"):
                counts[name] = conn.scalar(select(func.count()).select_from(tables[name]))
        if (
            header["revision"] != REVISION
            and not registry_was_exported
            and isinstance(footer, dict)
        ):
            for name in ("event_definitions", "event_definition_versions"):
                footer[name] = counts[name]
        metric_registry_was_exported = isinstance(footer, dict) and "metric_definitions" in footer
        if header["revision"] != REVISION and not metric_registry_was_exported:
            registry = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                from garmin_ai.metric_definitions import ensure_system_metric_definitions

                ensure_system_metric_definitions(registry, backfill=True)
                registry.commit()
            finally:
                registry.close()
            for name in ("metric_definitions", "metric_definition_versions"):
                counts[name] = conn.scalar(select(func.count()).select_from(tables[name]))
        if (
            header["revision"] != REVISION
            and not metric_registry_was_exported
            and isinstance(footer, dict)
        ):
            for name in ("metric_definitions", "metric_definition_versions"):
                footer[name] = counts[name]
            footer["event_metric_mappings"] = counts["event_metric_mappings"]
        registry = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            from garmin_ai.canonical_events import backfill_canonical_events

            backfill_canonical_events(registry)
            registry.commit()
        finally:
            registry.close()
        packs_were_exported = isinstance(footer, dict) and "module_configs" in footer
        if header["revision"] != REVISION and not packs_were_exported:
            registry = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                from garmin_ai.scenario_packs import ensure_scenario_packs

                ensure_scenario_packs(registry)
                registry.commit()
            finally:
                registry.close()
            counts["module_configs"] = conn.scalar(
                select(func.count()).select_from(tables["module_configs"])
            )
            if isinstance(footer, dict):
                footer["module_configs"] = counts["module_configs"]
        if header["revision"] != REVISION and isinstance(footer, dict):
            footer.setdefault("tracker_configs", 0)
        missing_neutral_tables = isinstance(footer, dict) and any(
            name not in footer for name in NEUTRAL_MESSAGE_TABLES
        )
        if missing_neutral_tables:
            upgrade_legacy_messages(conn, counts)
            for name in NEUTRAL_MESSAGE_TABLES:
                footer[name] = counts[name]
        if header["revision"] in {"bfccd06bf1c6", "4c9e28f110ab"} and isinstance(footer, dict):
            footer.setdefault("metric_observations", 0)
        if isinstance(footer, dict) and "measurement_revisions" not in footer:
            if header["revision"] != "b7c4e1a92d60":
                conn.execute(
                    text(
                        """
                        UPDATE measurements AS measurement
                        SET ingested_at = payload.fetched_at
                        FROM source_payloads AS payload
                        WHERE payload.id = measurement.source_ref
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    INSERT INTO measurement_revisions (
                        id, ts, metric, source, local_date, value, unit,
                        metric_definition_version_id, source_ref, quality, details, ingested_at
                    )
                    SELECT
                        gen_random_uuid(), ts, metric, source, local_date, value, unit,
                        metric_definition_version_id, source_ref, quality, details, ingested_at
                    FROM measurements
                    """
                )
            )
            counts["measurement_revisions"] = conn.scalar(
                select(func.count()).select_from(tables["measurement_revisions"])
            )
            footer["measurement_revisions"] = counts["measurement_revisions"]
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
        if before_activate is not None:
            before_activate()
    return counts


def file_revision(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def verify_encrypted_file(source: Path, key: bytes):
    """Authenticate filesystem-visible bytes without writing or returning plaintext."""
    with source.open("rb") as src:
        revision = file_revision(os.fstat(src.fileno()))
        size = revision[2]
        if size < len(MAGIC) + 12 + 16 or src.read(len(MAGIC)) != MAGIC:
            raise ValueError("Invalid encrypted backup")
        nonce = src.read(12)
        src.seek(-16, 2)
        tag = src.read(16)
        src.seek(len(MAGIC) + 12)
        remaining = size - len(MAGIC) - 12 - 16
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        digest = hashlib.sha256()
        plaintext_bytes = 0
        while remaining:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise ValueError("Encrypted backup ended unexpectedly")
            plaintext = decryptor.update(block)
            digest.update(plaintext)
            plaintext_bytes += len(plaintext)
            remaining -= len(block)
        tail = decryptor.finalize()
        digest.update(tail)
        plaintext_bytes += len(tail)
        if (
            src.read(16) != tag
            or src.read(1)
            or file_revision(os.fstat(src.fileno())) != revision
            or file_revision(source.stat()) != revision
        ):
            raise ValueError("Encrypted backup changed during verification")
    return {"plaintext_bytes": plaintext_bytes, "sha256": digest.hexdigest()}


def encrypt_file(source: Path, destination: Path, key: bytes):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination already exists")
    nonce = os.urandom(12)
    expected_hash = hashlib.sha256()
    expected_bytes = 0
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    ensure_parent(destination.parent)
    fd, name = tempfile.mkstemp(dir=destination.parent)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            dst.write(MAGIC + nonce)
            while block := src.read(CHUNK):
                expected_hash.update(block)
                expected_bytes += len(block)
                dst.write(encryptor.update(block))
            dst.write(encryptor.finalize())
            dst.write(encryptor.tag)
            dst.flush()
            os.fsync(dst.fileno())
        revision = file_revision(Path(name).stat())
        verified = verify_encrypted_file(Path(name), key)
        if verified != {"plaintext_bytes": expected_bytes, "sha256": expected_hash.hexdigest()}:
            raise ValueError("Encrypted backup differs from source stream")
        if file_revision(Path(name).stat()) != revision:
            raise ValueError("Encrypted backup changed before publication")
        publish_file(Path(name), destination)
        fsync_directory(destination.parent)
    finally:
        Path(name).unlink(missing_ok=True)
        fsync_directory(destination.parent)


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


@contextmanager
def plaintext_workspace(parent: Path):
    """Recover crash leftovers under a persistent cross-process operation lock."""
    from garmin_ai.storage_files import lock_descriptor, open_lock_file

    owned = private_directory(parent / ".garmin-ai-plaintext")
    descriptor = open_lock_file(owned / "operation.lock")
    try:
        lock_descriptor(descriptor)
        work = owned / "active"
        if has_path_redirect(work):
            raise ValueError("Plaintext workspace must not be redirected")
        if work.exists():
            shutil.rmtree(work)
        # Retry this barrier even if a preceding cleanup removed the entry.
        fsync_directory(owned)
        private_directory(work)
        try:
            yield work
        finally:
            shutil.rmtree(work)
            fsync_directory(owned)
            fsync_directory(parent)
    finally:
        os.close(descriptor)


def create_backup(engine, settings, destination: Path):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination already exists")
    for source in (settings.data_dir, settings.data_dir / "raw", settings.token_dir):
        if has_path_redirect(source):
            raise ValueError(
                "Backup source root is a symlink or junction, or has a redirected ancestor"
            )
    key = backup_key(settings)
    ensure_parent(destination.parent)
    for source in (settings.data_dir, settings.token_dir):
        if destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("Backup destination must be outside archived source trees")
    from garmin_ai.backup_space import require_backup_space

    # Plaintext staging stays beside the original local data, never on backup media.
    staging = private_directory(settings.data_dir / "backup-work")
    with plaintext_workspace(staging) as root:
        require_backup_space(engine, settings, destination)
        counts = export_database(engine, root / "database.jsonl.gz")
        # Recheck using the actual compressed export before allocating the tar.
        require_backup_space(
            engine,
            settings,
            destination,
            export_bytes=(root / "database.jsonl.gz").stat().st_size,
            export_staged=True,
        )
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
                        if path.is_symlink() or path.is_junction():
                            raise ValueError("Backup source contains a symlink or junction")
                        if path.is_file():
                            archive.add(
                                path,
                                arcname=str(Path(prefix) / path.relative_to(directory)),
                                recursive=False,
                            )
        encrypt_file(root / "backup.tar", destination, key)
    return counts


def publish_file(source: Path, destination: Path):
    """Publish exclusively, including on filesystems that cannot create hard links."""
    try:
        os.link(source, destination)
    except OSError as error:
        unsupported = error.errno in {errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM}
        # Windows ERROR_INVALID_FUNCTION maps to EINVAL on FAT/exFAT volumes.
        if not unsupported and not (sys.platform == "win32" and error.errno == errno.EINVAL):
            raise
        publish_directory(source, destination)


def publish_directory(source: Path, destination: Path):
    """Atomically publish a complete tree without replacing any directory entry."""
    if sys.platform == "win32":
        os.rename(source, destination)  # Windows rename is exclusive.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        argument_types = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            # Linux UAPI: arch/x86/entry/syscalls/syscall_64.tbl; asm-generic/unistd.h.
            number = {"x86_64": 316, "aarch64": 276}.get(platform.machine())
            if number is None or ctypes.sizeof(ctypes.c_void_p) != 8:
                raise NotImplementedError("No exclusive rename syscall for this Linux architecture")
            rename = libc.syscall
            argument_types = [ctypes.c_long, *argument_types]
            arguments = (number, *arguments)
        rename.argtypes = argument_types
    else:
        raise NotImplementedError("Exclusive directory publication is unsupported on this platform")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def unpack_backup(settings, source: Path, destination: Path):
    """Verify authentication before unpacking; never overwrites an existing directory."""
    for protected in (settings.data_dir, settings.token_dir):
        if destination.resolve().is_relative_to(
            protected.resolve()
        ) or protected.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Unpack into a separate recovery directory outside protected storage")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Unpack destination already exists")
    ensure_parent(destination.parent)
    with plaintext_workspace(destination.parent) as root:
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
                    dst.flush()
                    os.fsync(dst.fileno())
        # Child entries must be durable before publishing the recovery root.
        directories = [path for path in extracted.rglob("*") if path.is_dir()]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            fsync_directory(directory)
        fsync_directory(extracted)
        publish_directory(extracted, destination)
        fsync_directory(destination.parent)


def erase_all(engine, settings, confirmation: str):
    from garmin_ai.storage_files import exclusive_files

    with exclusive_files(settings, allow_erased=True):
        return _erase_all(engine, settings, confirmation)


def check_erasure_tree(root: Path):
    """Reject redirected descendants without traversing their external targets."""
    if has_path_redirect(root):
        raise ValueError("Unsafe erasure directory: redirected path component")
    pending = [root] if root.exists() else []
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or path.is_junction():
                    raise ValueError("Unsafe erasure directory: nested symlink or junction")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)


def _erase_all(engine, settings, confirmation: str):
    if confirmation != "ERASE ALL LOCAL HEALTH DATA":
        raise ValueError("Exact erasure confirmation required")
    for path in (settings.data_dir, settings.token_dir):
        if (
            (path.is_symlink() or path.is_junction())
            or path.resolve() == Path.home().resolve()
            or len(path.resolve().parts) < 4
            or path.resolve() == Path.cwd()
            or path.resolve() in Path.cwd().parents
        ):
            raise ValueError("Unsafe erasure directory")
        check_erasure_tree(path)
    # Require stopped workers; the lock is session-scoped until deletion completes.
    with engine.connect() as conn:
        if not conn.scalar(text("SELECT pg_try_advisory_lock(72104620)")):
            raise ValueError("Stop the runtime before erasing data")
        conn.commit()
        try:
            atomic_private_write(
                settings.lock_dir / "erased",
                b"Storage explicitly erased.\n",
                preserve_parent_mode=True,
            )
            with conn.begin():
                conn.execute(text("SELECT pg_advisory_xact_lock(72104622)"))
                names = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
                conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
                conn.execute(
                    text(
                        "INSERT INTO app_state (key, value) VALUES ('maintenance:erased', '{\"disabled\": true}'::jsonb)"
                    )
                )
            for path in (settings.data_dir, settings.token_dir):
                if path.exists():
                    if (
                        (path.is_symlink() or path.is_junction())
                        or path.resolve() == Path.home().resolve()
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
    # Repeat the flush even when a previous attempt already unlinked the old files.
    if directory.is_dir():
        fsync_directory(directory)


def scheduled_backup(engine, settings, destination: Path):
    """Recover a completed snapshot after a worker crash without overwriting it."""
    if destination.is_symlink():
        raise ValueError("Scheduled backup destination must not be a symlink")
    existed = destination.exists()
    if existed:
        staging = private_directory(settings.data_dir / "backup-work")
        with plaintext_workspace(staging) as work:
            decrypt_file(destination, work / "verified.tar", backup_key(settings))
        with destination.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        fsync_directory(destination.parent)
    else:
        create_backup(engine, settings, destination)
    prune_scheduled_backups(destination.parent, settings.backup_keep_daily, preserve=destination)
    return datetime.fromtimestamp(destination.stat().st_mtime, UTC)
