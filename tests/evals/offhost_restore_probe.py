"""Create or restore a synthetic encrypted backup on isolated CI hosts."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from check_backup_commands import cli_environment
from check_cutover_restore import (
    create_native_recovery_edit,
    native_api,
    pending_state,
    prepare_state,
    previous_api,
    verify_python_can_continue,
    wait_for_runtime_lease_release,
)
from check_database_export import table_values
from check_diary_http import NOTE, PATH, headers
from check_worker_cutover import lease, ready, wait_for_jobs, worker
from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import garmin_ai
from garmin_ai.accounts import profile_fingerprint
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
)
from garmin_ai.metric_definitions import aggregate_metric, record_observation
from garmin_ai.models import (
    AppState,
    Audit,
    Base,
    Event,
    EventDefinition,
    HealthDay,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    Person,
    SourcePayload,
)
from garmin_ai.operations import create_backup, restore_database, unpack_backup
from garmin_ai.replay import PARSER_VERSION, schedule_replay
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share, version_sharing_allowed
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


def engine_for_empty_test_database():
    url = make_url(os.environ["GA_TEST_DATABASE_URL"])
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not (url.database or "").endswith("_test")
        or url.query
    ):
        raise ValueError("An isolated local test database is required")
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as connection:
        if inspect(connection).get_table_names(schema="public"):
            raise ValueError("The test database must be newly created")
        config = Config("alembic.ini")
        config.set_main_option(
            "script_location", str(Path(garmin_ai.__file__).resolve().parent / "migrations")
        )
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        for table in Base.metadata.sorted_tables:
            count = connection.scalar(select(func.count()).select_from(table))
            if count != (1 if table.name == "people" else 0):
                raise ValueError("The test database must be empty")
    return engine


def settings(root, key):
    return Settings(
        _env_file=None,
        data_dir=root / "data",
        token_dir=root / "tokens",
        backup_dir=root / "backups",
        lock_dir=root / "locks",
        backup_key=key,
        database_url=os.environ["GA_TEST_DATABASE_URL"],
    )


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def database_digest(tables):
    serialized = json.dumps(tables, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def verify_python_reference(expected):
    package = Path(garmin_ai.__file__).resolve().parent
    revision, root = subprocess.check_output(
        ["git", "-C", str(package), "rev-parse", "HEAD", "--show-toplevel"], text=True
    ).splitlines()
    if revision != expected or package != Path(root).resolve() / "src" / "garmin_ai":
        raise ValueError("Imported Python must come from the frozen reference checkout")


def resume_restored_replay(
    engine, paths, work, pending, source_id, event_id, revision, *, native=None
):
    (job,) = pending["jobs"]
    assert job["status"] == "pending" and job["attempts"] == 0
    assert job["payload"]["raw_ref"] == str(source_id)
    assert lease(engine) is None
    environment = {
        **cli_environment(paths),
        "GA_BACKUP_KEY": "",
        "GA_INTEGRATIONS": "[]",
        "GA_TIMEZONE": "UTC",
        "PYTHONPATH": str(Path(garmin_ai.__file__).resolve().parents[1]),
    }
    command = (
        native
        if native is not None
        else Path(sys.executable).with_name("garmin-ai").resolve(strict=True)
    )
    started = monotonic()
    with worker(command, work, environment) as process:
        owner = ready(engine, process, native=native is not None)
        completed = wait_for_jobs(
            engine,
            process,
            lambda jobs: job["id"] in jobs and jobs[job["id"]].status == "done",
            seconds=25,
            failure="The recovered worker did not finish the restored replay",
        )
        resumed_seconds = round(monotonic() - started, 3)
        assert lease(engine) == owner
    wait_for_runtime_lease_release(engine)
    assert set(completed) == {job["id"]}
    applied = completed[job["id"]]
    assert applied.attempts == 1 and applied.completed_at is not None
    assert (
        applied.lease_token is None and applied.lease_until is None and applied.last_error is None
    )
    with Session(engine) as session:
        source = session.get(SourcePayload, source_id)
        day = session.get(HealthDay, date.fromisoformat(source.source_key))
        outcome = session.get(AppState, f"replay:{source_id}:{PARSER_VERSION}")
        assert source.parser_version == PARSER_VERSION and source.status == "normalized"
        assert day.steps == source.payload["totalSteps"]
        assert outcome.value["status"] == "normalized"
        assert session.get(Event, event_id).revision == revision
        data = LocalArchive(paths.data_dir / "raw").read(source.archive_key)
        assert hashlib.sha256(data).hexdigest() == source.payload_hash
        result = {
            "attempts": applied.attempts,
            "parser_version": source.parser_version,
            "steps": day.steps,
        }
    after_replay = pending_state(table_values(engine))
    for table in pending.keys() - {"jobs", "app_state"}:
        assert after_replay[table] == pending[table], table
    assert [
        row for row in after_replay["app_state"] if row["key"] != "replay:source_scan_cursor"
    ] == [row for row in pending["app_state"] if row["key"] != "replay:source_scan_cursor"]
    return {**result, "worker_resume_seconds": resumed_seconds}


def verify_native_resume(binary, engine, paths, work, pending, identity, *, started):
    event_id = UUID(identity["event_id"])
    with native_api(binary, work, paths) as api:
        api_ready_seconds = round(monotonic() - started, 3)
        event_path = f"{PATH}/{event_id}"
        retained = api.get(event_path, headers=headers("reader"))
        assert retained.status_code == 200
        event = retained.json()
        assert event["id"] == str(event_id) and event["revision"] == 1
        replay = resume_restored_replay(
            engine,
            paths,
            work,
            pending,
            UUID(identity["source_id"]),
            event_id,
            event["revision"],
            native=binary,
        )
        assert api.get("/health/ready").status_code == 200
        assert api.get(event_path, headers=headers("reader")).json() == event
    return {"api_ready_after_restore_seconds": api_ready_seconds, **replay}


def inspect_prewrite_recovery(binary, engine, paths, work, event_id, tables):
    event_path = f"{PATH}/{event_id}"
    assert lease(engine) is None
    with native_api(binary, work, paths, read_only=True) as inspector:
        assert lease(engine) is not None
        retained = inspector.get(event_path, headers=headers("reader"))
        assert retained.status_code == 200
        assert retained.json()["id"] == event_id and retained.json()["revision"] == 1
        assert inspector.post(PATH, json=NOTE, headers=headers("both")).status_code == 404
        assert inspector.put(event_path, json=NOTE, headers=headers("both")).status_code == 405
    wait_for_runtime_lease_release(engine)
    assert table_values(engine) == tables


def verify_prewrite_python_api(engine, paths, event_id):
    with previous_api(paths, engine) as previous:
        assert previous.get("/health/ready").status_code == 200
        retained = previous.get(f"{PATH}/{event_id}", headers=headers("reader"))
        assert retained.status_code == 200 and retained.json()["revision"] == 1
        request = headers("both", "synthetic-prewrite-rollback")
        created = previous.post(PATH, json=NOTE, headers=request)
        assert created.status_code == 200
        event = created.json()
        assert event["revision"] == 1 and event["id"] != event_id
        repeated = previous.post(PATH, json=NOTE, headers=request)
        assert repeated.status_code == 200 and repeated.json() == event
    with Session(engine) as session:
        identity = UUID(event["id"])
        assert session.get(Event, identity).revision == 1
        actions = session.scalars(select(Audit.action).where(Audit.event_id == identity)).all()
        assert actions == ["create"]
    return event["revision"]


def native_command(binary, paths, *arguments, succeeds=True):
    result = subprocess.run(
        [binary, *map(str, arguments)],
        env={
            "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
            "GA_DATA_DIR": str(paths.data_dir),
            "GA_TOKEN_DIR": str(paths.token_dir),
            "GA_LOCK_DIR": str(paths.lock_dir),
            "GA_BACKUP_DIR": str(paths.backup_dir),
            "GA_BACKUP_KEY": paths.backup_key.get_secret_value(),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    if succeeds and result.returncode != 0:
        raise RuntimeError(f"Native {arguments[0]} failed for the synthetic backup")
    if not succeeds and result.returncode != 1:
        raise RuntimeError("Native unpack did not reject the altered synthetic backup")
    return result.stdout


def make_backup(root, sha, *, native=None, python_reference=None):
    engine = engine_for_empty_test_database()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    paths = settings(root, key)
    payload = {"totalSteps": 1420}
    raw = LocalArchive(paths.data_dir / "raw")
    archive_key = raw.put_json(payload)
    versioned_file = f"raw/{archive_key}"
    raw_hash = hashlib.sha256(raw.read(archive_key)).hexdigest()
    paths.token_dir.mkdir(mode=0o700)
    token = paths.token_dir / "synthetic-token.json"
    token.write_bytes(b'{"fixture":"synthetic token placeholder"}')
    coverage = paths.data_dir / "coverage-report.json"
    coverage.write_bytes(b'{"source":"synthetic fixture"}')
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        assert session.scalar(select(Person)).singleton
        archived = SourcePayload(
            source="garmin_connect",
            endpoint="daily",
            source_key="2026-10-10",
            payload=payload,
            payload_hash=raw_hash,
            archive_key=archive_key,
            parser_version=PARSER_VERSION,
            status="normalized",
            fetched_at=now,
        )
        session.add(archived)
        session.flush()
        spec = DefinitionSpec(
            key="user.synthetic_offhost",
            labels={"en": "Synthetic restore check"},
            topology="point",
            privacy="sensitive",
            schema={
                "type": "object",
                "properties": {"note": {"type": "string", "maxLength": 40}},
                "required": ["note"],
                "additionalProperties": False,
            },
            fields={
                "note": {
                    "id": "user.synthetic_offhost.note",
                    "labels": {"en": "Note"},
                    "semantic": "text",
                }
            },
        )
        definition = create_definition_draft(session, spec, actor="synthetic", authorized=True)
        version = activate_definition(
            session, definition.id, definition.revision, actor="synthetic", authorized=True
        )
        event = create_custom_event(
            session,
            CustomEntryInput(
                definition_key=spec.key,
                start=now,
                timezone="UTC",
                status="needs_confirmation",
                values={"note": "synthetic"},
            ),
            actor="synthetic",
            idempotency_key="offhost:synthetic-entry",
        )
        grant_tracker_share(
            session,
            TrackerShareConsent(
                definition_id=definition.id,
                destination_kind="channel",
                destination_instance_id="telegram:primary",
                categories={"schema", "facts"},
                granted_at=now - timedelta(minutes=1),
            ),
            authorized=True,
        )
        tracker = TrackerSetupDraft(
            key="offhost_walk",
            name="Synthetic walk",
            locale="en",
            privacy="private",
            topology="point",
            fields=[
                TrackerFieldDraft(
                    key="minutes",
                    label="Minutes",
                    kind="number",
                    unit="minutes",
                    minimum=0,
                    maximum=240,
                )
            ],
        )
        tracker_preview = preview_tracker(session, tracker)
        confirm_tracker(
            session,
            TrackerConfirmation(
                draft=tracker, confirmation_token=tracker_preview["confirmation_token"]
            ),
            actor="synthetic",
        )
        metric = session.scalar(
            select(MetricDefinitionVersion)
            .join(MetricDefinition)
            .where(MetricDefinition.key == "user.offhost_walk.minutes")
        )
        observation = record_observation(
            session,
            metric,
            15,
            observed_at=now - timedelta(hours=1),
            source_ref=uuid4(),
            timezone="UTC",
            ingested_at=now,
        )
        identity = {
            "definition_id": str(definition.id),
            "version_id": str(version.id),
            "event_id": str(event.id),
            "owner_id": str(session.scalar(select(Person)).id),
            "metric_version_id": str(metric.id),
            "observation_id": str(observation.id),
            "observed_at": observation.observed_at.isoformat(),
            "ingested_at": observation.ingested_at.isoformat(),
            "source_id": str(archived.id),
        }
    source_id = UUID(identity["source_id"])
    prepare_state(engine, source_id, account=profile_fingerprint({"profileId": 12345}))
    native_edit = None
    if native is not None:
        with native_api(native, root, paths) as api:
            native_edit = create_native_recovery_edit(api)
    with Session(engine) as session, session.begin():
        session.get(SourcePayload, source_id).parser_version -= 1
        session.flush()
        assert schedule_replay(session, datetime.now(UTC)) == 1
    database_sha256 = database_digest(table_values(engine))
    backup = root / "synthetic-backup.enc"
    counts = (
        create_backup(engine, paths, backup)
        if native is None
        else json.loads(native_command(native, paths, "backup", backup))
    )
    descriptor = os.open(root / "synthetic-key.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(key)
    (root / "synthetic-manifest.json").write_text(
        json.dumps(
            {
                "commit": sha,
                "source_boot_id": boot_id(),
                "source_runtime": "python" if native is None else "rust",
                "source_python_reference": python_reference,
                "source_binary_sha256": digest(native) if native is not None else None,
                "counts": counts,
                "identity": identity,
                "database_sha256": database_sha256,
                "native_edit": native_edit,
                "files": {
                    versioned_file: raw_hash,
                    "tokens/synthetic-token.json": digest(token),
                    "coverage-report.json": digest(coverage),
                },
                "backup_sha256": digest(backup),
            }
        )
    )
    engine.dispose()
    print("Synthetic encrypted backup created.")


def verify_backup(
    root,
    work,
    sha,
    *,
    require_distinct_host=True,
    native=None,
    evidence=None,
    python_reference=None,
    prewrite_inspector=None,
):
    manifest = json.loads((root / "synthetic-manifest.json").read_text())
    if manifest["commit"] != sha:
        raise ValueError("Backup and restore must use the same source revision")
    if prewrite_inspector is not None and (
        native is not None
        or not python_reference
        or manifest["source_runtime"] != "python"
        or manifest["source_python_reference"] != python_reference
        or manifest["native_edit"] is not None
    ):
        raise ValueError(
            "Pre-write recovery requires a backup and restore from the same frozen Python runtime"
        )
    if require_distinct_host and manifest["source_boot_id"] == boot_id():
        raise ValueError("Restore must run on a different host")
    backup = root / "synthetic-backup.enc"
    if digest(backup) != manifest["backup_sha256"]:
        raise ValueError("Synthetic backup checksum mismatch")
    key = (root / "synthetic-key.txt").read_text()
    paths = settings(work, key)
    work.mkdir(parents=True, exist_ok=True)
    tampered = work / "tampered.enc"
    damaged = bytearray(backup.read_bytes())
    damaged[-1] ^= 1
    tampered.write_bytes(damaged)
    try:
        destination = work / "tampered-output"
        if native is not None:
            native_command(native, paths, "unpack-backup", tampered, destination, succeeds=False)
        else:
            try:
                unpack_backup(paths, tampered, destination)
            except InvalidTag:
                pass
            else:
                raise ValueError("Tampered synthetic backup was accepted")
        if destination.exists():
            raise ValueError("Altered backup left a published recovery directory")
    finally:
        tampered.unlink(missing_ok=True)
    engine = engine_for_empty_test_database()
    restored = work / "unpacked"
    started = monotonic()
    if native is None:
        unpack_backup(paths, backup, restored)
        counts = restore_database(engine, restored / "database.jsonl.gz")
    else:
        native_command(native, paths, "unpack-backup", backup, restored)
        counts = json.loads(
            native_command(native, paths, "restore-db", restored / "database.jsonl.gz")
        )
    if counts != manifest["counts"]:
        raise ValueError("Restored table counts differ")
    for filename, expected_hash in manifest["files"].items():
        path = (restored / filename).resolve(strict=True)
        if not path.is_relative_to(restored) or digest(path) != expected_hash:
            raise ValueError("A synthetic archive, token or configuration file changed")
    shutil.copytree(restored / "raw", paths.data_dir / "raw")
    shutil.copytree(restored / "tokens", paths.token_dir)
    shutil.copy2(restored / "coverage-report.json", paths.data_dir / "coverage-report.json")
    tables = table_values(engine)
    restored_digest = database_digest(tables)
    if restored_digest != manifest["database_sha256"]:
        raise ValueError("Restored database contents differ")
    ids = manifest["identity"]
    prewrite_result = None
    if prewrite_inspector is not None:
        inspect_prewrite_recovery(prewrite_inspector, engine, paths, work, ids["event_id"], tables)
        prewrite_result = {
            "native_inspector_sha256": digest(prewrite_inspector),
            "python_new_event_revision": verify_prewrite_python_api(engine, paths, ids["event_id"]),
        }
    archive = LocalArchive(paths.data_dir / "raw")
    with Session(engine) as session, session.begin():
        definition = session.get(EventDefinition, UUID(ids["definition_id"]))
        event = session.get(Event, UUID(ids["event_id"]))
        observation = session.get(MetricObservation, UUID(ids["observation_id"]))
        source = session.get(SourcePayload, UUID(ids["source_id"]))
        assert str(session.scalar(select(Person)).id) == ids["owner_id"]
        assert definition.key == "user.synthetic_offhost"
        assert str(event.definition_version_id) == ids["version_id"]
        assert event.payload["note"] == "synthetic" and event.status == "needs_confirmation"
        assert str(observation.metric_definition_version_id) == ids["metric_version_id"]
        assert observation.value == 15 and observation.valid
        assert source.payload_hash == manifest["files"][f"raw/{source.archive_key}"]
        assert hashlib.sha256(archive.read(source.archive_key)).hexdigest() == source.payload_hash
        observed = datetime.fromisoformat(ids["observed_at"])
        ingested = datetime.fromisoformat(ids["ingested_at"])
        start, end = observed - timedelta(hours=1), observed + timedelta(hours=1)
        assert (
            aggregate_metric(
                session,
                "user.offhost_walk.minutes",
                start,
                end,
                knowledge_cutoff=ingested - timedelta(seconds=1),
            )["observations"]
            == 0
        )
        assert (
            aggregate_metric(
                session,
                "user.offhost_walk.minutes",
                start,
                end,
                knowledge_cutoff=ingested + timedelta(seconds=1),
            )["value"]
            == 15
        )
        assert session.scalar(
            select(func.count()).select_from(Audit).where(Audit.event_id == event.id)
        )
        assert version_sharing_allowed(
            session,
            UUID(ids["version_id"]),
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema", "facts"},
        )
        new_event = create_custom_event(
            session,
            CustomEntryInput(
                definition_key=definition.key,
                start=datetime.now(UTC),
                timezone="UTC",
                status="needs_confirmation",
                values={"note": "synthetic after restore"},
            ),
            actor="synthetic",
            idempotency_key="offhost:after-restore",
        )
        new_event_id = new_event.id
    engine.dispose()
    with Session(engine) as restarted:
        assert restarted.get(Event, new_event_id).payload["note"] == "synthetic after restore"
        assert restarted.get(Event, UUID(ids["event_id"])).payload["note"] == "synthetic"
    verified_revisions = None
    restored_replay = None
    native_resume = None
    pending = pending_state(tables)
    assert all(pending.values())
    if native is not None:
        assert pending_state(table_values(engine)) == pending
        native_resume = verify_native_resume(
            native, engine, paths, work, pending, ids, started=started
        )
        print("Rust resumed the archived job and preserved retained work on the recovery host.")
    elif manifest["source_runtime"] == "rust" or prewrite_result is not None:
        event_id, revision = UUID(ids["event_id"]), 1
        if manifest["source_runtime"] == "rust":
            resumed = verify_python_can_continue(engine, paths, manifest["native_edit"])
            event_id, revision = UUID(resumed["id"]), resumed["revision"]
            verified_revisions = [1, manifest["native_edit"]["revision"], revision]
        assert pending_state(table_values(engine)) == pending
        restored_replay = resume_restored_replay(
            engine, paths, work, pending, UUID(ids["source_id"]), event_id, revision
        )
        print("Python resumed the archived job and preserved retained work on the recovery host.")
    engine.dispose()
    if evidence is not None:
        evidence.write_text(
            json.dumps(
                {
                    "commit": sha,
                    "source_runtime": manifest["source_runtime"],
                    "restore_runtime": "python" if native is None else "rust",
                    "source_python_reference": manifest["source_python_reference"],
                    "restore_python_reference": python_reference,
                    "source_binary_sha256": manifest["source_binary_sha256"],
                    "restore_binary_sha256": digest(native) if native is not None else None,
                    "distinct_host": manifest["source_boot_id"] != boot_id(),
                    "backup_sha256": manifest["backup_sha256"],
                    "verified_table_count": len(counts),
                    "verified_file_count": len(manifest["files"]),
                    "verified_database_sha256": restored_digest,
                    "verified_event_revisions_after_rollback": verified_revisions,
                    "verified_replay_after_rollback": restored_replay,
                    "verified_prewrite_rollback": prewrite_result,
                    "verified_native_resume_after_restore": native_resume,
                    "verified_restore_seconds": round(monotonic() - started, 3),
                },
                indent=2,
            )
            + "\n"
        )
    print("Synthetic backup restored with identical retained records and file digests.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "restore"))
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--native-binary", type=Path)
    parser.add_argument("--python-reference")
    parser.add_argument("--prewrite-inspector", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[0-9a-f]{40}", args.commit)
        or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != args.commit
    ):
        parser.error("commit must identify the checked-out source revision")
    native = args.native_binary.resolve(strict=True) if args.native_binary is not None else None
    if args.python_reference:
        if native is not None or not re.fullmatch(r"[0-9a-f]{40}", args.python_reference):
            parser.error("Specify a complete reference commit for a Python runtime")
        verify_python_reference(args.python_reference)
    if args.prewrite_inspector is not None and (
        args.mode != "restore" or not args.python_reference
    ):
        parser.error("--prewrite-inspector needs a restore using the frozen Python runtime")
    if args.mode == "create":
        make_backup(
            args.artifact.resolve(),
            args.commit,
            native=native,
            python_reference=args.python_reference,
        )
    else:
        if args.work is None:
            parser.error("restore needs --work")
        verify_backup(
            args.artifact.resolve(),
            args.work.resolve(),
            args.commit,
            native=native,
            evidence=args.evidence,
            python_reference=args.python_reference,
            prewrite_inspector=(
                args.prewrite_inspector.resolve(strict=True) if args.prewrite_inspector else None
            ),
        )


if __name__ == "__main__":
    main()
