import json
from datetime import UTC, datetime, timedelta

import pytest

from garmin_ai.access import permits_tool
from garmin_ai.device_history import history
from garmin_ai.models import Activity, ActivityPart, SourcePayload
from garmin_ai.normalize import PARSER_VERSION

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def activity(db, identity="synthetic", parsed="synthetic.fit"):
    row = Activity(
        id=identity,
        start=NOW,
        end=NOW + timedelta(hours=1),
        kind="running",
        timezone="UTC",
        fit_key="synthetic.fit",
        details={"parsed_fit_key": parsed},
    )
    db.add(row)
    db.add(
        SourcePayload(
            source="garmin_connect",
            endpoint="activity_fit",
            source_key=identity,
            archive_key=parsed,
            payload_hash="synthetic-" + identity,
            payload=None,
            parser_version=PARSER_VERSION,
            status="normalized",
        )
    )
    db.flush()
    return row


def part(db, kind, seq, payload, identity="synthetic"):
    db.add(ActivityPart(activity_id=identity, kind=kind, sequence=seq, payload=payload))
    db.flush()


def test_device_records_remain_distinct_and_private_fields_are_excluded(db):
    activity(db)
    for seq in (0, 1):
        part(
            db,
            "fit_device_info",
            seq,
            {
                "device_index": seq,
                "software_version": 4.2,
                "serial_number": "private-synthetic",
                "ant_device_number": 12345,
                "product_name": "private-synthetic",
                "product": "private-synthetic",
                "_fit": {"fields": ["private-synthetic"]},
            },
        )
    result = history(db, NOW, NOW + timedelta(days=1))
    records = result["rows"][0]["records"]
    assert len(records) == 2 and [r["sequence"] for r in records] == [0, 1]
    assert "private-synthetic" not in json.dumps(result)
    assert "ant_device_number" not in json.dumps(result)
    assert result["rows"][0]["evidence_status"] == "available"


def test_zone_snapshots_do_not_overwrite_other_activities(db):
    for identity, bpm in [("a", 140), ("b", 145)]:
        activity(db, identity)
        part(db, "fit_hr_zone", 0, {"high_bpm": bpm}, identity)
    rows = history(db, NOW, NOW + timedelta(days=1))["rows"]
    assert [r["records"][0]["fields"]["high_bpm"] for r in rows] == [140, 145]


def test_missing_and_stale_evidence_are_explicit(db):
    row = activity(db, parsed="old.fit")
    assert history(db, NOW, NOW + timedelta(days=1))["rows"][0]["evidence_status"] == "unavailable"
    part(db, "fit_device_info", 0, {"device_index": 0})
    assert history(db, NOW, NOW + timedelta(days=1))["rows"][0]["evidence_status"] == "stale"
    assert row.details["parsed_fit_key"] == "old.fit"


def test_activity_limit_and_scope(db):
    activity(db, "a")
    activity(db, "b")
    result = history(db, NOW, NOW + timedelta(days=1), limit=1)
    assert len(result["rows"]) == 1 and result["truncated"]
    assert permits_tool({"read:health"}, "device_history")
    assert not permits_tool({"read:diary"}, "device_history")
    with pytest.raises(ValueError):
        history(db, NOW, NOW + timedelta(days=367))


def test_message_sequence_precedes_kind_at_record_limit(db):
    activity(db)
    part(db, "fit_hr_zone", 0, {"high_bpm": 140})
    for i in range(1, 202):
        part(db, "fit_device_info", i, {"device_index": i})
    row = history(db, NOW, NOW + timedelta(days=1))["rows"][0]
    assert row["records"][0]["kind"] == "fit_hr_zone"
    assert row["records_truncated"]


def test_old_parser_with_same_archive_is_stale(db):
    from sqlalchemy import select

    activity(db)
    part(db, "fit_device_info", 0, {"device_index": 0})
    db.scalar(select(SourcePayload)).parser_version = 0
    db.flush()
    assert history(db, NOW, NOW + timedelta(days=1))["rows"][0]["evidence_status"] == "stale"


def test_large_valid_history_returns_bounded_partial_evidence(db):
    for i in range(100):
        identity = f"synthetic-{i:03}"
        activity(db, identity)
        part(db, "fit_device_info", 0, {"manufacturer": "x" * 80, "product": "y" * 80}, identity)
    result = history(db, NOW, NOW + timedelta(days=1))
    assert result["truncated"] and result["rows"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 24000
    from garmin_ai.llm import compact

    assert json.loads(compact(result))["rows"] == result["rows"]
