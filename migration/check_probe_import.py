"""Compare native coverage import with pinned Python on isolated databases."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_fit_replay import BAD_CRC, export_zip
from check_garmin_probe import OWNER, fake_garmin, stores
from sqlalchemy import text

from garmin_ai.accounts import AccountError, ensure_account, profile_fingerprint
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.sync import import_probe

AT = datetime(2026, 11, 1, 12, tzinfo=UTC)
DATE = AT.date().isoformat()
ZONE = "Europe/Bratislava"


def prepare():
    python = isolated_engine()
    original = os.environ["GA_TEST_DATABASE_URL"]
    try:
        current = os.environ["GA_TEST_RUST_DATABASE_URL"]
        assert current != original
        os.environ["GA_TEST_DATABASE_URL"] = current
        native = isolated_engine()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = original
    return python, native


def fixture(root, fingerprint):
    archive = LocalArchive(root / "data" / "raw")
    activity = {
        "activityId": 42,
        "startTimeGMT": "2026-11-01 09:30:00",
        "duration": 90.25,
        "activityType": {"typeKey": "running"},
        "timeZoneUnitDTO": {"timeZone": ZONE},
        "distance": 400.25,
    }
    requests = []

    def row(endpoint, key, archive_key, seconds=None, status="available"):
        requests.append(
            {
                "endpoint": endpoint,
                "key": key,
                "status": status,
                "archive_key": archive_key,
                "fetched_at": (AT + timedelta(seconds=seconds)).isoformat()
                if seconds is not None
                else None,
            }
        )
        os.utime(archive.root / archive_key, (AT.timestamp(), AT.timestamp()))

    row("daily", DATE, archive.put_json({"totalSteps": 123, "restingHeartRate": 57}), 1)
    row("sleep", DATE, archive.put_json([]), 2, status="empty")
    row("all_day_events", DATE, archive.put_json({"invented": ["redacted", 0]}))
    activities = archive.put_json([activity])
    row("activities", "recent", activities, 3)
    row("activity", "42", archive.put_json(activity), 4)
    row("activity_details", "42", archive.put_json({"size": 0, "polylines": []}), 5)
    row("activity_fit", "42", archive.put_bytes(export_zip(), "zip"), 6)
    requests.append({"endpoint": "intensity", "key": DATE, "status": "error"})
    report = {
        "account_fingerprint": fingerprint,
        "activity_list_archive": activities,
        "requests": requests,
        "complete": False,
    }
    path = root / "data" / "coverage-report.json"
    path.write_text(json.dumps(report))
    return path, report, archive


def snapshot(engine):
    queries = (
        """SELECT endpoint,source_key,payload_hash,archive_key,status,parser_version,
                  fetched_at,payload::text FROM source_payloads ORDER BY endpoint,source_key,payload_hash""",
        "SELECT day,steps,resting_hr FROM health_days ORDER BY day",
        'SELECT id,start,"end",kind,timezone,distance_m,fit_key FROM activities ORDER BY id',
        "SELECT activity_id,kind,sequence,payload::text FROM activity_parts ORDER BY 1,2,3",
        "SELECT count(*) FROM audit_log",
    )
    with engine.connect() as connection:
        return [connection.execute(text(query)).all() for query in queries]


def invoke(prefix, engine, root, environment, *arguments):
    result = subprocess.run(
        [*prefix, "import-probe", *arguments],
        cwd=root,
        env={**environment, "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)},
        capture_output=True,
        timeout=30,
        text=True,
        check=False,
    )
    return result


def verify(binary, root, python, native):
    fingerprint = profile_fingerprint(OWNER)
    for database in (python, native):
        ensure_account(database, fingerprint)
    path, report, archive = fixture(root, fingerprint)
    env = {
        "GA_DATA_DIR": str(root / "data"),
        "GA_LOCK_DIR": str(root / "state"),
        "GA_TOKEN_DIR": str(root / "tokens"),
        "GA_BACKUP_DIR": str(root / "backups"),
        "GA_TIMEZONE": ZONE,
    }
    old, new = [sys.executable, "-m", "garmin_ai.cli"], [str(binary)]
    for arguments in ((), ("--report", str(path))):
        outputs = [
            invoke(command, engine, root, env, *arguments)
            for command, engine in ((old, python), (new, native))
        ]
        for result in outputs:
            assert result.returncode == 0 and not result.stderr, result.stderr
        assert [json.loads(result.stdout) for result in outputs] == [
            {"imported": 7, "errors": []},
            {"imported": 7, "errors": []},
        ]
        expected, actual = snapshot(python), snapshot(native)
        for index, (reference, candidate) in enumerate(zip(expected, actual, strict=True)):
            assert reference == candidate, (index, reference, candidate)
        assert len(actual[0]) == 8 and len(actual[2]) == 1 and len(actual[3]) >= 2
        assert actual[1] == [(AT.date(), 123, 57)]

    existing = snapshot(native)
    wrong = path.with_name("wrong-owner.json")
    wrong.write_text(json.dumps(report | {"account_fingerprint": "3" * 64}))
    invalid = invoke(new, native, root, env, "--report", str(wrong))
    assert invalid.returncode == 1 and not invalid.stdout and existing == snapshot(native)
    assert (root / "data" / "raw" / report["activity_list_archive"]).is_file()

    legacy_report = report.copy()
    legacy_report.pop("account_fingerprint")
    wrong.write_text(json.dumps(legacy_report))
    denied = invoke(new, native, root, env, "--report", str(wrong))
    assert denied.returncode == 1 and not denied.stdout and existing == snapshot(native)
    with fake_garmin(root) as (ca, seen, _state):
        token_env, token_file = stores(root, ca)
        token_bytes = token_file.read_bytes()
        for invalid_provenance in (None, "", 0, False, "f" * 63, "g" * 64):
            wrong.write_text(json.dumps(report | {"account_fingerprint": invalid_provenance}))
            try:
                import_probe(
                    python,
                    archive,
                    Settings(_env_file=None, timezone=ZONE),
                    wrong,
                    confirmed_legacy_fingerprint=fingerprint,
                )
            except AccountError:
                pass
            else:
                raise AssertionError("Frozen import accepted invalid report provenance")
            malformed = invoke(
                new, native, root, token_env, "--report", str(wrong), "--confirm-legacy-owner"
            )
            assert malformed.returncode == 1 and not malformed.stdout, invalid_provenance
            assert snapshot(python) == snapshot(native) == existing
            assert token_file.read_bytes() == token_bytes
        seen.clear()
        wrong.write_text(json.dumps(legacy_report))
        confirmed = invoke(
            new, native, root, token_env, "--report", str(wrong), "--confirm-legacy-owner"
        )
        assert confirmed.returncode == 0, confirmed.stderr
        assert json.loads(confirmed.stdout) == {"imported": 7, "errors": []}
        assert seen == ["profile"] and token_file.read_bytes() == token_bytes
    expected = import_probe(
        python,
        archive,
        Settings(_env_file=None, timezone=ZONE),
        wrong,
        confirmed_legacy_fingerprint=fingerprint,
    )
    assert expected == {"imported": 7, "errors": []} and existing == snapshot(native)
    print("Matched coverage import, durable FIT/JSON data, retries and original-owner confirmation")
    print("Rejected all six invalid explicit owner fields and accepted a confirmed legacy omission")

    fit = archive.put_bytes(BAD_CRC, "zip")
    damaged = {
        "account_fingerprint": fingerprint,
        "requests": [
            {
                "endpoint": "activity_fit",
                "key": "42",
                "status": "available",
                "archive_key": fit,
                "fetched_at": (AT + timedelta(minutes=1)).isoformat(),
            }
        ],
        "complete": False,
    }
    wrong.write_text(json.dumps(damaged))
    for command, database in ((old, python), (new, native)):
        result = invoke(command, database, root, env, "--report", str(wrong))
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert "Some archived data needs parser corrections." in result.stderr
        assert json.loads(result.stdout) == {
            "imported": 1,
            "errors": [{"endpoint": "activity_fit", "error_type": "FitCRCError"}],
        }
    failed = snapshot(native)
    assert snapshot(python) == failed
    assert failed[1:] == existing[1:] and len(failed[0]) == len(existing[0]) + 1
    assert any(row[0] == "activity_fit" and row[4] == "error" for row in failed[0])
    print("Matched corrupt FIT reports while preserving previous projections")

    tampered = archive.put_bytes(b"synthetic original archive object", "zip")
    (archive.root / tampered).write_bytes(b"synthetic substituted archive bytes")
    archive_files = sorted(path for path in archive.root.rglob("*") if path.is_file())
    wrong.write_text(
        json.dumps(damaged | {"requests": [damaged["requests"][0] | {"archive_key": tampered}]})
    )
    result = invoke(new, native, root, env, "--report", str(wrong))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {
        "imported": 1,
        "errors": [{"endpoint": "activity_fit", "error_type": "ValueError"}],
    }
    assert snapshot(native) == failed
    assert archive_files == sorted(path for path in archive.root.rglob("*") if path.is_file())
    print("Rejected substituted FIT bytes before recording a source or publishing a new object")

    valid_fit = report["requests"][-2]["archive_key"]
    unavailable_fit = "99/" + "9" * 64 + ".zip"
    damaged["requests"] = [
        damaged["requests"][0] | {"archive_key": unavailable_fit},
        damaged["requests"][0] | {"key": "8888", "archive_key": valid_fit},
    ]
    wrong.write_text(json.dumps(damaged))
    for command, database in ((old, python), (new, native)):
        result = invoke(command, database, root, env, "--report", str(wrong))
        assert result.returncode == 1, (result.stdout, result.stderr)
        actual = json.loads(result.stdout)
        assert actual == {
            "imported": 2,
            "errors": [
                {"endpoint": "activity_fit", "error_type": "FileNotFoundError"},
                {"endpoint": "activity_fit", "error_type": "LookupError"},
            ],
        }, actual
        assert snapshot(database) == failed

        with database.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO module_configs
                    (id, owner_id, pack_key, tracking_enabled, collection_enabled,
                     reminders_enabled, visible, llm_enabled, settings, revision)
                SELECT gen_random_uuid(), id, 'training', false, false,
                       false, false, false, '{}', 1 FROM people
                """)
            )
        disabled = invoke(command, database, root, env, "--report", str(wrong))
        assert disabled.returncode == 0 and not disabled.stderr, disabled.stderr
        assert json.loads(disabled.stdout) == {"imported": 0, "errors": []}
        assert snapshot(database) == failed
    print("Matched missing FIT objects, missing activities and disabled collection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    python, native = prepare()
    try:
        with TemporaryDirectory() as directory:
            verify(args.binary.resolve(strict=True), Path(directory), python, native)
    finally:
        python.dispose()
        native.dispose()


if __name__ == "__main__":
    main()
