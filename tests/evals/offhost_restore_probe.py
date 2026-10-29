"""Create or restore a synthetic encrypted backup on isolated CI hosts."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

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
    Audit,
    Base,
    Event,
    EventDefinition,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    Person,
)
from garmin_ai.operations import create_backup, restore_database, unpack_backup
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
        backup_key=key,
    )


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def make_backup(root, sha):
    engine = engine_for_empty_test_database()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        assert session.scalar(select(Person)).singleton
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
        }
    paths = settings(root, key)
    backup = root / "synthetic-backup.enc"
    counts = create_backup(engine, paths, backup)
    descriptor = os.open(root / "synthetic-key.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(key)
    (root / "synthetic-manifest.json").write_text(
        json.dumps(
            {
                "commit": sha,
                "source_boot_id": boot_id(),
                "counts": counts,
                "identity": identity,
                "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
            }
        )
    )
    engine.dispose()
    print("Synthetic encrypted backup created.")


def verify_backup(root, work, sha, *, require_distinct_host=True):
    manifest = json.loads((root / "synthetic-manifest.json").read_text())
    if manifest["commit"] != sha:
        raise ValueError("Backup and restore must use the same source revision")
    if require_distinct_host and manifest["source_boot_id"] == boot_id():
        raise ValueError("Restore must run on a different host")
    backup = root / "synthetic-backup.enc"
    if hashlib.sha256(backup.read_bytes()).hexdigest() != manifest["backup_sha256"]:
        raise ValueError("Synthetic backup checksum mismatch")
    key = (root / "synthetic-key.txt").read_text()
    paths = settings(work, key)
    work.mkdir(parents=True, exist_ok=True)
    tampered = work / "tampered.enc"
    damaged = bytearray(backup.read_bytes())
    damaged[-1] ^= 1
    tampered.write_bytes(damaged)
    try:
        unpack_backup(paths, tampered, work / "tampered-output")
    except InvalidTag:
        pass
    else:
        raise ValueError("Tampered synthetic backup was accepted")
    finally:
        tampered.unlink(missing_ok=True)
    engine = engine_for_empty_test_database()
    restored = work / "unpacked"
    unpack_backup(paths, backup, restored)
    counts = restore_database(engine, restored / "database.jsonl.gz")
    if counts != manifest["counts"]:
        raise ValueError("Restored table counts differ")
    with Session(engine) as session, session.begin():
        ids = manifest["identity"]
        definition = session.get(EventDefinition, UUID(ids["definition_id"]))
        event = session.get(Event, UUID(ids["event_id"]))
        observation = session.get(MetricObservation, UUID(ids["observation_id"]))
        assert str(session.scalar(select(Person)).id) == ids["owner_id"]
        assert definition.key == "user.synthetic_offhost"
        assert str(event.definition_version_id) == ids["version_id"]
        assert event.payload["note"] == "synthetic" and event.status == "needs_confirmation"
        assert str(observation.metric_definition_version_id) == ids["metric_version_id"]
        assert observation.value == 15 and observation.valid
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
    engine.dispose()
    print("Synthetic backup restored; source and destination boot IDs differ.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "restore"))
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[0-9a-f]{40}", args.commit)
        or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != args.commit
    ):
        parser.error("commit must identify the checked-out source revision")
    if args.mode == "create":
        make_backup(args.artifact.resolve(), args.commit)
    else:
        if args.work is None:
            parser.error("restore needs --work")
        verify_backup(args.artifact.resolve(), args.work.resolve(), args.commit)


if __name__ == "__main__":
    main()
