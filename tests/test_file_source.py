"""Local source import preserves provenance and owner edits across replay."""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from garmin_ai.events import Conflict
from garmin_ai.extension_tck import check_source_adapter
from garmin_ai.file_source import (
    MAX_ROWS,
    FileSourceAdapter,
    _read_bounded,
    _read_rows,
    apply,
    build_plan,
    preview,
)
from garmin_ai.models import AppState, Event, EventDefinition, SourcePayload
from garmin_ai.natural_language import process_tracker_text
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


def tracker(db, *, topology="point", field_kind="integer", maximum=10):
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
                    "kind": field_kind,
                    "unit": "count",
                    "minimum": 0,
                    "maximum": maximum,
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
                "decimal_separator": ".",
                "null_markers": [""],
            }
        )
    )
    return source, mapping


def test_csv_row_limit_rejects_many_small_rows():
    content = "id\n" + "a\n" * (MAX_ROWS + 1)
    with pytest.raises(ValueError, match="1 to 500 object rows"):
        _read_rows(content, "csv")


def test_csv_rejects_missing_trailing_field_instead_of_importing_null():
    with pytest.raises(ValueError, match="header width"):
        _read_rows("id,when,note\na,2026-10-07T09:00:00+02:00\n", "csv")
    assert _read_rows("id,when,note\na,2026-10-07T09:00:00+02:00,\n", "csv")[0]["note"] == ""


def test_json_rejects_numbers_that_change_during_parsing():
    with pytest.raises(ValueError, match="supported precision"):
        _read_rows('[{"score": 0.12345678901234567890}]', "json")
    assert _read_rows('[{"score": 0.1}]', "json") == [{"score": 0.1}]


def test_preview_rejects_lossy_csv_numbers(db, tmp_path):
    tracker(db, field_kind="number", maximum=10**18)
    source, mapping = files(tmp_path, value="9007199254740993")
    assert preview(build_plan(db, source, mapping))["errors"] == [
        {"row": 1, "code": "numeric_precision"}
    ]
    source, mapping = files(tmp_path, value="0.12345678901234567890")
    assert preview(build_plan(db, source, mapping))["errors"] == [
        {"row": 1, "code": "numeric_precision"}
    ]
    source, mapping = files(tmp_path, value="0.1")
    assert preview(build_plan(db, source, mapping))["error_count"] == 0


@pytest.mark.parametrize("value", ["1e10000000", "-1e10000000", "1e-10000000"])
def test_preview_rejects_extreme_integer_exponents_before_conversion(db, tmp_path, value):
    tracker(db)
    source, mapping = files(tmp_path, value=value)
    assert preview(build_plan(db, source, mapping))["errors"] == [
        {"row": 1, "code": "numeric_format"}
    ]


@pytest.mark.parametrize("value", ["1__0", "1_.2", " 1", "1 "])
def test_preview_rejects_nonstandard_numeric_syntax(db, tmp_path, value):
    tracker(db)
    source, mapping = files(tmp_path, value=value)
    assert preview(build_plan(db, source, mapping))["errors"] == [
        {"row": 1, "code": "numeric_format"}
    ]


def test_preview_reports_timezone_conversion_overflow_as_redacted_row_error(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path, timestamp="9999-12-31T23:59:59-12:00")
    config = json.loads(mapping.read_text())
    config["timezone"] = "Etc/GMT+12"
    mapping.write_text(json.dumps(config))

    report = preview(build_plan(db, source, mapping))
    assert report["errors"] == [{"row": 1, "code": "timestamp_range"}]
    assert "9999-12-31" not in json.dumps(report)


def test_file_reads_are_bounded_and_reject_special_files(tmp_path):
    oversized = tmp_path / "growing.csv"
    oversized.write_bytes(b"x" * 1025)
    with pytest.raises(ValueError, match="size limit"):
        _read_bounded(oversized, 1024)
    with pytest.raises(ValueError, match="regular files"):
        _read_bounded(Path("/dev/zero"), 1024)


def test_mapping_requires_explicit_decimal_and_null_conventions(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    for omitted in ("decimal_separator", "null_markers"):
        changed = json.loads(mapping.read_text())
        changed.pop(omitted)
        mapping.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="Invalid mapping file"):
            build_plan(db, source, mapping)
        source, mapping = files(tmp_path)


def test_preview_redacts_jsonb_incompatible_null_characters(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    source.write_text("id,when,score,extra\na,2026-10-07T09:00:00+02:00,3,hidden\x00text\n")
    plan = build_plan(db, source, mapping)
    assert preview(plan)["errors"] == [{"row": 1, "code": "jsonb_null_character"}]
    assert "hidden" not in json.dumps(preview(plan))
    with pytest.raises(ValueError, match="Fix preview errors"):
        apply(db, plan, plan.plan_hash)

    source.write_text(
        json.dumps(
            [
                {
                    "id": "a",
                    "when": "2026-10-07T09:00:00+02:00",
                    "score": 3,
                    "extra": {"nested": "hidden\x00text"},
                }
            ]
        )
    )
    changed = json.loads(mapping.read_text())
    changed["format"] = "json"
    mapping.write_text(json.dumps(changed))
    assert build_plan(db, source, mapping).issues == [{"row": 1, "code": "jsonb_null_character"}]

    source.write_text(
        json.dumps(
            [
                {
                    "id": "a",
                    "when": "2026-10-07T09:00:00+02:00",
                    "score": 3,
                    "extra": {"nested": "\ud800"},
                }
            ]
        )
    )
    assert build_plan(db, source, mapping).issues == [{"row": 1, "code": "jsonb_invalid_unicode"}]


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


def test_imported_provenance_survives_text_correction(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    plan = build_plan(db, source, mapping)
    apply(db, plan, plan.plan_hash)
    event = db.scalar(select(Event).where(Event.kind == "user.energy_import"))
    text = "Change energy to 4 count"

    class FixedProvider:
        def structured(self, _instruction, _prompt, schema):
            return schema.model_validate(
                {
                    "schema_version": "tracker.nl.v1",
                    "intent": "update_entry",
                    "definition_version_id": str(event.definition_version_id),
                    "event_id": str(event.id),
                    "fields": [
                        {
                            "field_id": "user.energy_import.energy",
                            "value": 4,
                            "unit": "count",
                            "evidence": {
                                "start": text.index("4"),
                                "end": text.index("4") + 1,
                                "quote": "4",
                            },
                            "unit_evidence": {
                                "start": text.index("count"),
                                "end": text.index("count") + len("count"),
                                "quote": "count",
                            },
                        }
                    ],
                    "confidence": 0.99,
                }
            )

    result = process_tracker_text(
        db,
        FixedProvider(),
        {"text": text, "operation_id": "text-correction", "selected_event_id": event.id},
        granted={"read:diary", "write:diary"},
        actor="owner",
        timezone="Europe/Bratislava",
    )
    db.refresh(event)
    assert result["written"] is True
    assert event.payload["energy"] == 4
    assert any(ref.get("file_sha256") == plan.file_hash for ref in event.evidence_refs)
    assert any(ref.get("field_id") == "user.energy_import.energy" for ref in event.evidence_refs)


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


def test_confirmation_binds_to_active_tracker_version(db, tmp_path, monkeypatch):
    from garmin_ai import file_source

    tracker(db)
    source, mapping = files(tmp_path)
    original = build_plan(db, source, mapping)
    original_form_for_action = file_source.form_for_action

    def newer_version(session, action_id):
        form = original_form_for_action(session, action_id)
        action = form.action.model_copy(update={"definition_version_id": uuid4()})
        return form.model_copy(update={"action": action})

    monkeypatch.setattr(file_source, "form_for_action", newer_version)
    changed = build_plan(db, source, mapping)
    assert changed.plan_hash != original.plan_hash
    with pytest.raises(Conflict, match="preview again"):
        apply(db, changed, original.plan_hash)


def test_apply_rechecks_tracker_version_after_write_lock(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    plan = build_plan(db, source, mapping)
    definition = db.scalar(
        select(EventDefinition).where(EventDefinition.key == "user.energy_import")
    )
    definition.current_version += 1
    db.flush()
    with pytest.raises(Conflict, match="Tracker version changed"):
        apply(db, plan, plan.plan_hash)
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_reapply_holds_definition_row_through_skip(db, db_engine, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    plan = build_plan(db, source, mapping)
    assert apply(db, plan, plan.plan_hash)["created"] == 1
    db.commit()

    replay = build_plan(db, source, mapping)
    assert apply(db, replay, replay.plan_hash)["skipped"] == 1
    with Session(db_engine) as contender:
        with pytest.raises(OperationalError):
            contender.execute(
                select(EventDefinition.id)
                .where(EventDefinition.key == "user.energy_import")
                .with_for_update(nowait=True)
            ).all()
        contender.rollback()


def test_apply_includes_latest_representable_timestamp(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path, timestamp="9999-12-31T23:59:59.999999+00:00")
    config = json.loads(mapping.read_text())
    config["timezone"] = "UTC"
    mapping.write_text(json.dumps(config))
    plan = build_plan(db, source, mapping)
    assert preview(plan)["error_count"] == 0
    assert apply(db, plan, plan.plan_hash)["created"] == 1


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


def test_open_interval_tracker_requires_bounded_file_rows(db, tmp_path):
    tracker(db, topology="open_interval")
    source, mapping = files(tmp_path)
    with pytest.raises(ValueError, match="explicit end column"):
        build_plan(db, source, mapping)

    source.write_text(
        "id,when,until,score\na,2026-01-01T09:00:00+01:00,2026-01-01T09:30:00+01:00,3\n"
    )
    config = json.loads(mapping.read_text())
    config["end_column"] = "until"
    mapping.write_text(json.dumps(config))
    plan = build_plan(db, source, mapping)
    assert plan.issues == []
    assert FileSourceAdapter(plan).capabilities.time_semantics == "interval"

    source.write_text(
        "id,when,until,score\na,2026-01-01T09:00:00+01:00,2026-01-01T09:00:00+01:00,3\n"
    )
    assert build_plan(db, source, mapping).issues == [
        {"row": 1, "code": "end_after_start_required"}
    ]


def test_point_tracker_rejects_end_column_mapping(db, tmp_path):
    tracker(db)
    source, mapping = files(tmp_path)
    config = json.loads(mapping.read_text())
    config["end_column"] = "until"
    mapping.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Point trackers cannot use an end column"):
        build_plan(db, source, mapping)


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
