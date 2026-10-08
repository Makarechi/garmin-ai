"""Local source import preserves provenance and owner edits across replay."""

import json
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from garmin_ai.events import Conflict
from garmin_ai.extension_tck import check_source_adapter
from garmin_ai.file_source import (
    MAX_ROWS,
    FileSourceAdapter,
    _read_rows,
    apply,
    build_plan,
    preview,
)
from garmin_ai.models import AppState, Event, SourcePayload
from garmin_ai.queries import list_events, timeline
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerSetupDraft,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)


def tracker(db, *, topology="point"):
    draft = TrackerSetupDraft.model_validate(
        {
            "key": "energy_import",
            "name": "Energy import",
            "locale": "en",
            "topology": topology,
            "fields": [
                {
                    "key": "energy",
                    "label": "Energy",
                    "kind": "integer",
                    "unit": "count",
                    "minimum": 0,
                    "maximum": 10,
                }
            ],
        }
    )
    token = preview_tracker(db, draft)["confirmation_token"]
    confirm_tracker(db, TrackerConfirmation(draft=draft, confirmation_token=token), actor="test")


def files(tmp_path, *, device="watch_a", value="3", timestamp="2026-10-07T09:00:00+02:00"):
    source = tmp_path / f"{device}.csv"
    source.write_text(f"id,when,score\na,{timestamp},{value}\n")
    mapping = tmp_path / f"{device}.mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "format": "csv",
                "source_instance_id": "synthetic_source",
                "device_id": device,
                "definition_key": "user.energy_import",
                "row_id_column": "id",
                "start_column": "when",
                "timezone": "Europe/Bratislava",
                "field_columns": {"energy": "score"},
                "units": {"energy": "count"},
            }
        )
    )
    return source, mapping


def test_csv_row_limit_rejects_many_small_rows():
    content = "id\n" + "a\n" * (MAX_ROWS + 1)
    with pytest.raises(ValueError, match="1 to 500 object rows"):
        _read_rows(content, "csv")


def test_file_source_preview_apply_replay_and_owner_correction(db, db_engine, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    plan = build_plan(db, source, mapping)
    report = preview(plan)
    assert report["valid_count"] == 1 and report["errors"] == []
    assert report["mapping"]["timezone"] == "Europe/Bratislava"
    assert report["mapping"]["fields"] == [
        {
            "name": "energy",
            "column": "score",
            "input": "integer",
            "unit": "count",
            "minimum": 0,
            "maximum": 10,
        }
    ]
    assert (
        FileSourceAdapter(plan)
        .read_page(
            start=datetime.fromisoformat("2026-10-07T00:00:00+00:00"),
            end=datetime.fromisoformat("2026-10-08T00:00:00+00:00"),
            cursor=None,
            limit=100,
        )
        .next_cursor
        is None
    )
    with pytest.raises(Conflict, match="preview again"):
        apply(db, plan, "0" * 64)
    assert apply(db, plan, report["plan_sha256"])["created"] == 1
    event = db.scalar(select(Event).where(Event.kind == "user.energy_import"))
    assert event.source == "file_import" and event.payload["energy"] == 3
    assert event.evidence_refs[0]["file_sha256"] == report["file_sha256"]
    start = datetime.fromisoformat("2026-10-07T00:00:00+00:00")
    end = datetime.fromisoformat("2026-10-08T00:00:00+00:00")
    assert list_events(db, start, end, kind="user.energy_import")["rows"][0]["source"] == (
        "file_import"
    )
    assert timeline(db, start, end)["layers"]["context"][0]["source"] == "file_import"
    assert db.scalar(select(func.count()).select_from(SourcePayload)) == 1
    correction = form_for_action(db, f"edit:{event.id}:{event.revision}")
    updated = submit_form(
        db,
        correction.id,
        FormSubmission(
            action_id=correction.id,
            schema_hash=correction.schema_hash,
            start=event.start,
            timezone=event.timezone,
            values={"energy": 5},
            units={"energy": "count"},
        ),
        actor="owner",
    )
    assert updated.payload["energy"] == 5
    assert updated.evidence_refs[0]["file_sha256"] == report["file_sha256"]
    db.commit()
    with Session(db_engine) as restarted, restarted.begin():
        replay = build_plan(restarted, source, mapping)
        assert apply(restarted, replay, report["plan_sha256"])["skipped"] == 1
    db.expire_all()
    assert db.scalar(select(Event).where(Event.kind == "user.energy_import")).payload["energy"] == 5
    changed_source, changed_mapping = files(tmp_path, value="4")
    with pytest.raises(Conflict, match="Source row or mapping changed"):
        apply(
            db,
            build_plan(db, changed_source, changed_mapping),
            preview(build_plan(db, changed_source, changed_mapping))["plan_sha256"],
        )
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_replay_rejects_changed_mapping_for_same_source_row(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    original = build_plan(db, source, mapping)
    assert apply(db, original, original.plan_hash)["created"] == 1

    changed = json.loads(mapping.read_text())
    changed["decimal_separator"] = ","
    mapping.write_text(json.dumps(changed))
    remapped = build_plan(db, source, mapping)
    with pytest.raises(Conflict, match="Source row or mapping changed"):
        apply(db, remapped, remapped.plan_hash)
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_file_source_requires_explicit_time_unit_and_device_identity(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path, timestamp="2026-10-07T09:00:00")
    plan = build_plan(db, source, mapping)
    assert plan.issues == [{"row": 1, "code": "timestamp_offset_required"}]
    with pytest.raises(ValueError, match="Fix preview errors"):
        apply(db, plan, plan.plan_hash)
    source, mapping = files(tmp_path, timestamp="2026-10-07T09:00:00+00:00")
    assert build_plan(db, source, mapping).issues[0]["code"] == "timestamp_timezone_mismatch"
    data = json.loads(mapping.read_text())
    data["units"] = {}
    mapping.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exact unit"):
        build_plan(db, source, mapping)


def test_file_source_does_not_merge_devices_with_same_row_and_time(db, tmp_path):
    tracker(db)
    for device in ("watch_a", "watch_b"):
        source, mapping = files(tmp_path, device=device)
        plan = build_plan(db, source, mapping)
        assert apply(db, plan, plan.plan_hash)["created"] == 1
    assert db.scalar(select(func.count()).select_from(Event)) == 2
    assert (
        db.scalar(
            select(func.count())
            .select_from(AppState)
            .where(AppState.key.startswith("file-import:row:"))
        )
        == 2
    )


def test_json_source_paginates_and_rejects_duplicate_or_nonfinite_rows(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    source.write_text(
        json.dumps(
            [
                {"id": "a", "when": "2026-10-07T09:00:00+02:00", "score": 3},
                {"id": "b", "when": "2026-10-07T10:00:00+02:00", "score": 4},
            ]
        )
    )
    config = json.loads(mapping.read_text())
    config["format"] = "json"
    mapping.write_text(json.dumps(config))
    plan = build_plan(db, source, mapping)
    adapter = FileSourceAdapter(plan)
    start = datetime.fromisoformat("2026-10-07T00:00:00+00:00")
    end = datetime.fromisoformat("2026-10-08T00:00:00+00:00")
    first = adapter.read_page(start=start, end=end, cursor=None, limit=1)
    second = adapter.read_page(start=start, end=end, cursor=first.next_cursor, limit=1)
    assert [item.source_record_id for item in first.records + second.records] == ["a", "b"]
    assert first.next_cursor == "1" and second.next_cursor is None
    assert apply(db, plan, plan.plan_hash)["created"] == 2

    source.write_text(
        json.dumps(
            [
                {"id": "a", "when": "2026-10-07T09:00:00+02:00", "score": 3},
                {"id": "a", "when": "2026-10-07T10:00:00+02:00", "score": 4},
            ]
        )
    )
    duplicate = build_plan(db, source, mapping)
    assert duplicate.issues == [{"row": 2, "code": "duplicate_source_id"}]
    source.write_text('[{"id":"a","when":"2026-10-07T09:00:00+02:00","score":1e9999}]')
    with pytest.raises(ValueError, match="non-finite"):
        build_plan(db, source, mapping)


def test_file_source_implements_extension_contract(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path, timestamp="2026-01-01T09:00:00+01:00")
    result = check_source_adapter(
        FileSourceAdapter(build_plan(db, source, mapping)), instance_id="synthetic_source:watch_a"
    )
    assert result["records"] == 1


def test_file_source_interval_contract_requires_and_preserves_end(db, tmp_path):
    tracker(db, topology="bounded_interval")
    source, mapping = files(tmp_path, timestamp="2026-01-01T09:00:00+01:00")
    source.write_text(
        "id,when,until,score\na,2026-01-01T09:00:00+01:00,2026-01-01T09:30:00+01:00,3\n"
    )
    config = json.loads(mapping.read_text())
    config["end_column"] = "until"
    mapping.write_text(json.dumps(config))

    plan = build_plan(db, source, mapping)
    assert plan.issues == []
    adapter = FileSourceAdapter(plan)
    assert adapter.capabilities.time_semantics == "interval"
    assert plan.records[0].effective_end == datetime.fromisoformat("2026-01-01T09:30:00+01:00")
    overlapping = adapter.read_page(
        start=datetime.fromisoformat("2026-01-01T09:15:00+01:00"),
        end=datetime.fromisoformat("2026-01-01T10:00:00+01:00"),
        cursor=None,
        limit=100,
    )
    assert [record.source_record_id for record in overlapping.records] == ["a"]
    assert check_source_adapter(adapter, instance_id="synthetic_source:watch_a")["records"] == 1
    assert apply(db, plan, plan.plan_hash)["created"] == 1
    assert db.scalar(select(Event).where(Event.kind == "user.energy_import")).end == (
        datetime.fromisoformat("2026-01-01T09:30:00+01:00")
    )

    source.write_text("id,when,until,score\na,2026-01-01T09:00:00+01:00,,3\n")
    assert build_plan(db, source, mapping).issues == [{"row": 1, "code": "end_required"}]


def test_preview_error_report_never_echoes_row_values(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path, value="private-health-value-do-not-repeat")
    report = preview(build_plan(db, source, mapping))
    assert report["error_count"] == 1
    assert report["errors"] == [{"row": 1, "code": "numeric_format"}]
    assert "private-health-value" not in json.dumps(report)


def test_invalid_mapping_error_does_not_echo_private_values(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    config = json.loads(mapping.read_text())
    config["private_note"] = "private-health-value-do-not-repeat"
    mapping.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Invalid mapping file") as error:
        build_plan(db, source, mapping)
    assert "private-health-value" not in str(error.value)
