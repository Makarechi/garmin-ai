"""Verify a late overlapping sample through public reads, backup, restore and replay."""

import argparse
import hashlib
import json
import os
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from check_backup_commands import provision_engine, run_cli, settings
from check_cutover_restore import native_api, previous_api
from check_database_export import table_values
from check_diary_http import headers
from check_health_publication import run as compare_publications
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.archive import LocalArchive
from garmin_ai.models import Job
from garmin_ai.replay import PARSER_VERSION, schedule_replay

OVERLAP = datetime.fromtimestamp(1792888380, UTC)
HEART_RATE = "heart_rate_bpm"


def read_overlap(api, value, source=None):
    response = api.post(
        "/tools/metric_series",
        headers=headers("health"),
        json={
            "arguments": {
                "metric": HEART_RATE,
                "start": OVERLAP.isoformat(),
                "end": (OVERLAP + timedelta(seconds=1)).isoformat(),
                "minutes": 1,
            }
        },
    )
    assert response.status_code == 200
    document = response.json()
    (row,) = document["rows"]
    assert row["samples"] == 1 and row["min"] == row["max"] == value
    if source is not None:
        assert row["source_refs"] == [str(source)]
    return document


def overlap_state(engine):
    params = {"sample": OVERLAP, "metric": HEART_RATE}
    with engine.connect() as connection:
        current = connection.execute(
            text("SELECT value, source_ref FROM measurements WHERE ts=:sample AND metric=:metric"),
            params,
        ).one()
        revisions = connection.execute(
            text("""
                 SELECT value, source_ref FROM measurement_revisions
                 WHERE ts=:sample AND metric=:metric ORDER BY value
                 """),
            params,
        ).all()
    assert [row.value for row in revisions] == [39, 82]
    assert current == revisions[1] and current.source_ref is not None
    assert revisions[0].source_ref is not None
    return current, revisions


def restore_snapshot(binary, fixture, root, source, target, origin, recovered):
    archive = LocalArchive(source.data_dir / "raw")
    for item in json.loads(fixture.read_text()):
        archive.put_json(item["payload"])
    with origin.connect() as connection:
        objects = connection.execute(
            text("SELECT DISTINCT archive_key, payload_hash FROM source_payloads")
        ).all()
    assert objects
    for key, digest in objects:
        assert hashlib.sha256(archive.read(key)).hexdigest() == digest

    source.token_dir.mkdir(parents=True, mode=0o700)
    snapshot = root / "intraday.enc"
    expected = table_values(origin)
    run_cli(binary, root, source, "backup", snapshot)
    unpacked = root / "unpacked"
    run_cli(binary, root, target, "unpack-backup", snapshot, unpacked)
    run_cli(binary, root, target, "restore-db", unpacked / "database.jsonl.gz")
    assert table_values(recovered) == expected
    copied = LocalArchive(target.data_dir / "raw")
    for key, digest in objects:
        path = unpacked / "raw" / key
        assert path.stat().st_mode & 0o777 == 0o600
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest
        assert copied.put_bytes(body, "json") == key
    target.token_dir.mkdir(parents=True, mode=0o700)
    return len(expected), len(objects)


def replay_overlap(binary, root, recovered, config, source):
    # Preserve the legacy queue order when the older sample becomes eligible.
    with Session(recovered) as session, session.begin():
        assert schedule_replay(session, datetime.now(UTC) - timedelta(minutes=1)) == 1
        queued = session.execute(select(Job.id, Job.payload).where(Job.kind == "raw_replay")).one()
        assert queued.payload["raw_ref"] != str(source)

    with recovered.begin() as connection:
        current = connection.scalar(
            text("SELECT parser_version FROM source_payloads WHERE id=:id"), {"id": source}
        )
        assert current == PARSER_VERSION
        connection.execute(
            text("UPDATE source_payloads SET parser_version=:previous WHERE id=:id"),
            {"id": source, "previous": PARSER_VERSION - 1},
        )
    result = run_cli(binary, root, config, "replay", "--once")
    assert "Archive replay completed" in result.splitlines()
    with recovered.connect() as connection:
        assert connection.execute(
            text("SELECT status, attempts FROM jobs WHERE id=:id"), {"id": queued.id}
        ).one() == ("done", 1)
        assert (
            connection.scalar(
                text("SELECT parser_version FROM source_payloads WHERE id=:id"), {"id": source}
            )
            == PARSER_VERSION - 1
        )

    result = run_cli(binary, root, config, "replay", "--once")
    assert "Archive replay completed" in result.splitlines()
    with recovered.connect() as connection:
        parser = connection.scalar(
            text("SELECT parser_version FROM source_payloads WHERE id=:id"), {"id": source}
        )
        outcome = connection.scalar(
            text("SELECT value->>'status' FROM app_state WHERE key=:key"),
            {"key": f"replay:{source}:{PARSER_VERSION}"},
        )
        jobs = connection.execute(
            text(
                "SELECT status, attempts FROM jobs WHERE kind='raw_replay' AND payload->>'raw_ref'=:id"
            ),
            {"id": str(source)},
        ).all()
    assert parser == PARSER_VERSION and outcome in {"normalized", "unchanged"}
    assert jobs == [("done", 1)]
    return outcome


def verify(publication, binary, fixture):
    reference_url = os.environ["GA_TEST_DATABASE_URL"]
    native_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    recovery_url = os.environ["GA_TEST_RESTORE_DATABASE_URL"]
    addresses = [make_url(value) for value in (reference_url, native_url, recovery_url)]
    reference = addresses[0]
    assert len({address.database for address in addresses}) == 3
    assert all(address.database and address.database.endswith("_test") for address in addresses)
    assert all(address == reference.set(database=address.database) for address in addresses)
    compare_publications(publication, fixture)

    with ExitStack() as stack:
        legacy, native = (create_engine(url, hide_parameters=True) for url in addresses[:2])
        stack.callback(legacy.dispose)
        stack.callback(native.dispose)
        recovered = provision_engine(stack, recovery_url)
        root = Path(stack.enter_context(TemporaryDirectory(prefix="intraday-recovery-")))
        legacy_config = settings(root / "legacy", legacy)
        source_config = settings(root / "native", native)
        target_config = settings(root / "recovery", recovered)
        current, revisions = overlap_state(native)
        with previous_api(legacy_config, legacy) as api:
            read_overlap(api, 39)
        with native_api(binary, root, source_config) as api:
            expected = read_overlap(api, 82, current.source_ref)
        tables, files = restore_snapshot(
            binary, fixture, root, source_config, target_config, native, recovered
        )
        with previous_api(target_config, recovered) as api:
            assert read_overlap(api, 82, current.source_ref) == expected
        outcome = replay_overlap(binary, root, recovered, target_config, revisions[0].source_ref)
        assert overlap_state(recovered) == (current, revisions)
        with native_api(binary, root, target_config) as api:
            assert read_overlap(api, 82, current.source_ref) == expected
    print(
        json.dumps(
            {
                "python_published_value": 39,
                "native_and_restored_value": 82,
                "python_reads_restored_native_value": True,
                "retained_values": [39, 82],
                "restored_tables": tables,
                "restored_archive_files": files,
                "replay_outcome": outcome,
                "preceding_replay_jobs_completed": 1,
                "replay_attempts": 1,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("publication_test", type=Path)
    parser.add_argument("binary", type=Path)
    options = parser.parse_args()
    fixtures = Path(__file__).with_name("fixtures") / "garmin_health_publication.json"
    verify(
        options.publication_test.resolve(strict=True),
        options.binary.resolve(strict=True),
        fixtures,
    )
