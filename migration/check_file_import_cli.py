"""Exercise file import handover and rollback on an isolated reference database."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_tracker_actions import seed
from sqlalchemy import text

MAPPING = {
    "format": "csv",
    "source_instance_id": "synthetic-source",
    "device_id": "synthetic-device",
    "definition_key": "user.active_actions",
    "row_id_column": "row",
    "start_column": "time",
    "timezone": "Europe/Bratislava",
    "field_columns": {"description": "notes"},
    "decimal_separator": ".",
    "null_markers": ["secret-like-synthetic-marker"],
}
DATE = "2026-03-29T03:10:00+02:00"
ENTRY = "Synthetic personal value 🧪"


def run(prefix, root, mode, filename, mapping="mapping.json", confirm=None, status=0):
    command = [*prefix, "import-file", mode, filename, mapping]
    if confirm is not None:
        command += ["--confirm", confirm]
    result = subprocess.run(
        command,
        cwd=root,
        env={"GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"]},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == status, (mode, status, result.stderr)
    assert all(
        secret not in result.stdout + result.stderr
        for secret in (ENTRY, "secret-like-synthetic-marker", "synthetic-owner-row-id")
    )
    return json.loads(result.stdout) if result.stdout else None


def imported(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT e.payload->>'description' AS description,e.revision,e.source,
                       p.status,p.parser_version,a.actor,s.value->>'event_id'=e.id::text AS receipt
                FROM events e JOIN source_payloads p
                    ON e.evidence_refs @> jsonb_build_array(jsonb_build_object('source_payload_id',p.id::text))
                JOIN audit_log a ON a.event_id=e.id AND a.action='create'
                JOIN app_state s ON s.key='file-import:row:'||p.source_key
                    AND s.value->>'row_hash'=p.payload_hash
                WHERE e.source='file_import' ORDER BY p.source,e.id
                """
            )
        ).all()


def exercise(engine, binary, root):
    python = [sys.executable, "-m", "garmin_ai.cli"]
    native = [str(binary)]
    csv = f"row,time,notes\r\nsynthetic-owner-row-id,{DATE},{ENTRY}\r\n"
    (root / "data.csv").write_bytes(csv.encode("utf-8"))
    (root / "mapping.json").write_text(json.dumps(MAPPING), encoding="utf-8")
    preview = run(python, root, "preview", "data.csv")
    assert preview == run(native, root, "preview", "data.csv")
    confirmed = preview["plan_sha256"]
    assert preview["valid_count"] == 1 and preview["error_count"] == 0
    assert not imported(engine)
    run(native, root, "apply", "data.csv", status=2)
    run(native, root, "apply", "data.csv", confirm="0" * 64, status=1)
    assert not imported(engine)

    assert run(python, root, "apply", "data.csv", confirm=confirmed)["created"] == 1
    assert run(native, root, "apply", "data.csv", confirm=confirmed)["skipped"] == 1
    assert len(imported(engine)) == 1

    unprojected = 0
    for _ in range(220):
        unprojected = [unprojected]
    payload = [
        {
            "row": "synthetic-json-id",
            "time": DATE,
            "notes": "Synthetic JSON note",
            "retained": unprojected,
        }
    ]
    (root / "data.json").write_text(json.dumps(payload), encoding="utf-8")
    document = MAPPING | {"format": "json", "device_id": "synthetic-json-device"}
    (root / "json-map.json").write_text(json.dumps(document), encoding="utf-8")
    preview = run(python, root, "preview", "data.json", "json-map.json")
    assert preview == run(native, root, "preview", "data.json", "json-map.json")
    assert (
        run(native, root, "apply", "data.json", "json-map.json", preview["plan_sha256"])["created"]
        == 1
    )
    assert (
        run(python, root, "apply", "data.json", "json-map.json", preview["plan_sha256"])["skipped"]
        == 1
    )
    stored = imported(engine)
    assert len(stored) == 2
    assert {row.description for row in stored} == {ENTRY, "Synthetic JSON note"}
    assert all(
        row[1:] == (1, "file_import", "applied", 0, "file_import:synthetic-source", True)
        for row in stored
    )

    failed = (
        f"row,time,notes\r\nnew-synthetic-row,{DATE},Synthetic first row\r\n"
        f"synthetic-owner-row-id,{DATE},Changed synthetic value\r\n"
    )
    (root / "changed.csv").write_bytes(failed.encode())
    run(native, root, "apply", "changed.csv", confirm=confirmed, status=1)
    current = run(native, root, "preview", "changed.csv")
    run(native, root, "apply", "changed.csv", confirm=current["plan_sha256"], status=1)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM source_payloads WHERE source LIKE 'file_import:%'")
            )
            == 2
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM app_state WHERE key LIKE 'file-import:row:%'")
            )
            == 2
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM app_state WHERE key LIKE 'file-import:cursor:%'")
            )
            == 2
        )
    assert imported(engine) == stored

    invalid = f"row,time,notes\r\ninvalid,2026-03-29T03:10+00:00,{ENTRY}\r\nmissing,{DATE},\r\n"
    (root / "invalid.csv").write_bytes(invalid.encode())
    invalid_preview = run(python, root, "preview", "invalid.csv", status=2)
    assert invalid_preview == run(native, root, "preview", "invalid.csv", status=2)
    assert invalid_preview["error_count"] > 0
    run(native, root, "apply", "invalid.csv", confirm=invalid_preview["plan_sha256"], status=1)
    assert imported(engine) == stored
    os.mkfifo(root / "fifo")
    run(native, root, "preview", "fifo", status=1)
    print("Matched file and mapping confirmations, cross-runtime retries and whole-file rollback")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed(engine)
        with TemporaryDirectory() as root:
            exercise(engine, args.binary.resolve(strict=True), Path(root))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
