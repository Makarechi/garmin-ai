import json
import struct
from datetime import UTC, datetime, timedelta

import pytest
from fitdecode.types import (
    BASE_TYPES,
    DevField,
    DevFieldDefinition,
    Field,
    FieldData,
    FieldDefinition,
)
from fitdecode.utils import compute_crc
from sqlalchemy import event, select

from garmin_ai.archive import LocalArchive
from garmin_ai.fit import message_values, parse_fit, store_fit
from garmin_ai.models import Activity, ActivityPart, SourcePayload

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def test_default_details_exclude_new_and_unknown_sample_families_before_pagination(db):
    from garmin_ai.queries import activity_details

    db.add(
        Activity(
            id="synthetic",
            start=NOW,
            end=NOW + timedelta(minutes=30),
            kind="running",
            timezone="UTC",
        )
    )
    db.flush()
    kinds = ["fit_accelerometer_data", "fit_hr", "fit_monitoring", "fit_unknown_999"]
    for kind in kinds:
        for sequence in range(30):
            db.add(
                ActivityPart(
                    activity_id="synthetic",
                    kind=kind,
                    sequence=sequence,
                    payload={"synthetic": [1, 2]},
                )
            )
    db.add(
        ActivityPart(
            activity_id="synthetic",
            kind="fit_session",
            sequence=0,
            payload={"total_distance": 1000},
        )
    )
    db.flush()
    result = activity_details(db, "synthetic")
    assert [p["kind"] for p in result["parts"]] == ["fit_session"]
    assert not result["truncated"]
    all_parts = activity_details(db, "synthetic", include_samples=True, limit=200)
    assert len(all_parts["parts"]) == 121


def fit_bytes(value):
    # A valid synthetic FIT containing a device_info message and native device_index.
    body = struct.pack("<BBBHB", 0x40, 0, 0, 23, 1) + bytes([0, 1, 2, 0, value])
    header = struct.pack("<BBHI4s", 14, 0x10, 2100, len(body), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    return header + body + struct.pack("<H", compute_crc(header + body))


def test_real_synthetic_fit_keeps_previously_filtered_device_messages():
    rows = parse_fit(fit_bytes(1))
    assert len(rows) == 1
    kind, values = rows[0]
    assert kind == "device_info"
    assert values["device_index"] == 1
    assert values["_fit"]["global_message_number"] == 23
    assert values["_fit"]["fields"][0]["field_number"] == 0


def local_time_fit_bytes(seconds):
    # user_profile.wake_time is a uint32 localtime_into_day field.
    body = struct.pack("<BBBHB", 0x40, 0, 0, 3, 1) + bytes([28, 4, 0x86, 0])
    body += struct.pack("<I", seconds)
    header = struct.pack("<BBHI4s", 14, 0x10, 2100, len(body), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    return header + body + struct.pack("<H", compute_crc(header + body))


def test_fit_local_clock_keeps_raw_seconds_and_serializes_to_json(db, tmp_path):
    data = local_time_fit_bytes(7 * 3600 + 15 * 60)
    kind, values = parse_fit(data)[0]
    assert kind == "user_profile" and values["wake_time"] == "07:15:00"
    assert values["_fit"]["fields"][0]["raw_value"] == 7 * 3600 + 15 * 60
    assert json.loads(json.dumps(values))["wake_time"] == "07:15:00"
    db.add(
        Activity(
            id="local-clock",
            kind="running",
            start=NOW,
            end=NOW + timedelta(minutes=30),
            timezone="UTC",
        )
    )
    db.flush()
    result = store_fit(db, LocalArchive(tmp_path), "local-clock", data, fetched_at=NOW)
    assert result["status"] == "normalized"
    part = db.scalar(select(ActivityPart).where(ActivityPart.activity_id == "local-clock"))
    assert part.payload["wake_time"] == "07:15:00"


def test_native_and_developer_names_do_not_overwrite_each_other():
    from types import SimpleNamespace

    base = BASE_TYPES[2]
    native = Field("heart_rate", base, 3, units="bpm")
    developer = DevField(1, "heart_rate", 3, base, "score", 3)
    native_data = FieldData(FieldDefinition(native, 3, base, 1), native, None, 70, 70)
    developer_data = FieldData(DevFieldDefinition(developer, 1, 3, 1), developer, None, 9, 9)
    values = message_values(
        SimpleNamespace(
            fields=[developer_data, native_data],
            global_mesg_num=20,
            local_mesg_num=0,
            is_developer_data=True,
        ),
        4,
    )
    assert values["heart_rate"] == 70
    assert [field["value"] for field in values["_fit"]["fields"]] == [9, 70]
    assert values["_fit"]["fields"][0]["developer_data_index"] == 1
    assert values["_fit"]["fields"][0]["units"] == "score"


def test_unchanged_fit_skips_parser_and_part_writes_but_a_b_a_reparses(db, tmp_path, monkeypatch):
    db.add(
        Activity(
            id="1", kind="swimming", start=NOW, end=NOW + timedelta(minutes=30), timezone="UTC"
        )
    )
    db.flush()
    archive = LocalArchive(tmp_path)
    first, second = fit_bytes(1), fit_bytes(2)
    assert store_fit(db, archive, "1", first, fetched_at=NOW)["status"] == "normalized"
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if "activity_parts" in statement.lower():
            statements.append(statement)

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", capture)
    original = parse_fit
    monkeypatch.setattr(
        "garmin_ai.fit.parse_fit",
        lambda data: (_ for _ in ()).throw(AssertionError("unchanged FIT parsed")),
    )
    try:
        assert (
            store_fit(db, archive, "1", first, fetched_at=NOW + timedelta(seconds=1))["status"]
            == "unchanged"
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
        monkeypatch.setattr("garmin_ai.fit.parse_fit", original)
    assert statements == []
    assert (
        store_fit(db, archive, "1", second, fetched_at=NOW + timedelta(seconds=2))["status"]
        == "normalized"
    )
    assert (
        store_fit(db, archive, "1", first, fetched_at=NOW + timedelta(seconds=3))["status"]
        == "normalized"
    )
    assert db.scalar(select(ActivityPart)).payload["device_index"] == 1
    assert db.get(Activity, "1").kind == "swimming"
    current = db.scalar(
        select(SourcePayload).where(SourcePayload.archive_key == db.get(Activity, "1").fit_key)
    )
    current.parser_version -= 1
    assert (
        store_fit(db, archive, "1", first, fetched_at=NOW + timedelta(seconds=4))["status"]
        == "normalized"
    )


def test_default_details_preserve_events_workouts_and_strength_sets(db):
    from garmin_ai.queries import activity_details

    db.add(
        Activity(
            id="structured",
            kind="strength_training",
            start=NOW,
            end=NOW + timedelta(minutes=30),
            timezone="UTC",
        )
    )
    db.flush()
    families = {
        "fit_event",
        "fit_workout",
        "fit_workout_step",
        "fit_set",
        "fit_length",
        "fit_segment_lap",
        "fit_time_in_zone",
        "fit_exercise_title",
        "fit_training_settings",
        "fit_hr_zone",
        "fit_dive_summary",
        "fit_split_summary",
    }
    for kind in families | {"fit_record", "fit_hr", "fit_unknown", "fit_jump", "fit_segment_point"}:
        db.add(
            ActivityPart(activity_id="structured", kind=kind, sequence=0, payload={"synthetic": 1})
        )
    db.flush()
    assert {part["kind"] for part in activity_details(db, "structured")["parts"]} == families
    for sequence in range(1, 150):
        db.add(
            ActivityPart(activity_id="structured", kind="fit_jump", sequence=sequence, payload={})
        )
    db.flush()
    assert {part["kind"] for part in activity_details(db, "structured")["parts"]} == families
    assert "fit_jump" in {
        part["kind"] for part in activity_details(db, "structured", include_samples=True)["parts"]
    }


@pytest.mark.parametrize("record_count", [0, 1, 500, 501, 1261])
def test_fit_replacement_batches_writes_and_preserves_order(
    db, tmp_path, monkeypatch, record_count
):
    from garmin_ai import fit

    db.add(Activity(id="batch-fit", kind="running", start=NOW, end=NOW, timezone="UTC"))
    db.flush()
    db.add_all(
        [
            ActivityPart(activity_id="batch-fit", kind="fit_old", sequence=0, payload={}),
            ActivityPart(activity_id="batch-fit", kind="lap", sequence=0, payload={"keep": True}),
        ]
    )
    db.flush()
    records = [
        ("record" if i % 2 else "event", {"synthetic_index": i}) for i in range(record_count)
    ]
    monkeypatch.setattr(fit, "parse_fit", lambda _data: records)
    inserts = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO activity_parts"):
            inserts.append(statement)

    event.listen(db.connection(), "before_cursor_execute", capture)
    try:
        result = store_fit(db, LocalArchive(tmp_path), "batch-fit", fit_bytes(1), fetched_at=NOW)
    finally:
        event.remove(db.connection(), "before_cursor_execute", capture)
    assert result["status"] == "normalized" and result["rows"] == record_count
    assert len(inserts) == (record_count + 499) // 500
    parts = list(
        db.scalars(
            select(ActivityPart)
            .where(ActivityPart.activity_id == "batch-fit", ActivityPart.kind.startswith("fit_"))
            .order_by(ActivityPart.sequence)
        )
    )
    assert [(part.kind, part.sequence, part.payload) for part in parts] == [
        (f"fit_{kind}", i, payload) for i, (kind, payload) in enumerate(records)
    ]
    assert db.scalar(select(ActivityPart).where(ActivityPart.kind == "lap")).payload == {
        "keep": True
    }
    assert bool(db.info.get("projection_changed")) is bool(record_count)


def test_fit_later_batch_failure_restores_previous_projection(db, tmp_path, monkeypatch):
    from garmin_ai import fit

    db.add(Activity(id="rollback-fit", kind="running", start=NOW, end=NOW, timezone="UTC"))
    db.flush()
    archive = LocalArchive(tmp_path)
    first = store_fit(db, archive, "rollback-fit", fit_bytes(1), fetched_at=NOW)
    assert first["status"] == "normalized"
    previous = db.get(Activity, "rollback-fit").fit_key
    monkeypatch.setattr(
        fit, "parse_fit", lambda _data: [("record", {"index": i}) for i in range(1001)]
    )
    inserts = []

    def fail_second(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO activity_parts"):
            inserts.append(statement)
            if len(inserts) == 2:
                raise RuntimeError("synthetic failed second FIT batch")

    event.listen(db.connection(), "before_cursor_execute", fail_second)
    try:
        result = store_fit(
            db, archive, "rollback-fit", fit_bytes(2), fetched_at=NOW + timedelta(seconds=1)
        )
    finally:
        event.remove(db.connection(), "before_cursor_execute", fail_second)
    assert result["status"] == "error" and result["error_type"] == "RuntimeError"
    assert len(inserts) == 2
    parts = list(db.scalars(select(ActivityPart).where(ActivityPart.activity_id == "rollback-fit")))
    assert len(parts) == 1 and parts[0].payload["device_index"] == 1
    activity = db.get(Activity, "rollback-fit", populate_existing=True)
    assert activity.fit_key == previous and activity.details["fit_status"] == "error"
    source = db.get(SourcePayload, result["source_ref"])
    assert source.status == "error"
