import struct
from datetime import UTC, datetime, timedelta

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
