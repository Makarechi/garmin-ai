from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError

from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    ensure_system_definition,
    propose_definition_revision,
    update_custom_event,
)
from garmin_ai.events import EventInput, create_event, delete_event, undo_last, update_event
from garmin_ai.metric_definitions import (
    CoveragePolicy,
    MetricSpec,
    aggregate_metric,
    bind_event_field,
    convert_unit,
    ensure_system_metric_definitions,
    measurement_rows_as_of,
    record_observation,
    register_metric_definition,
)
from garmin_ai.models import Measurement, MeasurementRevision, MetricObservation

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def focus_definition(*, maximum=5):
    unit = f"score_1-{maximum}"
    return DefinitionSpec(
        key="user.focus_session",
        labels={"en": "Focus session"},
        topology="bounded_interval",
        schema={
            "type": "object",
            "properties": {
                "focus": {"type": "integer", "minimum": 1, "maximum": maximum},
                "distractions": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
            "required": ["focus", "distractions"],
            "additionalProperties": False,
        },
        fields={
            "focus": {
                "id": "user.focus_session.focus",
                "labels": {"en": "Focus"},
                "semantic": "ordinal",
                "unit": unit,
            },
            "distractions": {
                "id": "user.focus_session.distractions",
                "labels": {"en": "Distractions"},
                "semantic": "count",
                "unit": "count",
            },
        },
    )


def focus_metric(*, maximum=5, scale_version=1):
    return MetricSpec(
        key="user.focus_session.focus",
        labels={"en": "Focus"},
        value_kind="ordinal",
        unit=f"score_1-{maximum}",
        dimension="ordinal",
        scale_id="user.focus",
        scale_version=scale_version,
        aggregation="distribution",
        allowed_methods={"distribution", "median", "latest"},
        coverage=CoveragePolicy(kind="sparse"),
        time_semantics="interval",
        minimum=1,
        maximum=maximum,
    )


def entry(value, *, start=NOW, distractions=0, status="confirmed"):
    return CustomEntryInput(
        definition_key="user.focus_session",
        start=start,
        end=start + timedelta(minutes=25),
        timezone="UTC",
        values={"focus": value, "distractions": distractions},
        status=status,
        units={"focus": f"score_1-{5 if value <= 5 else 10}", "distractions": "count"},
    )


def activate_focus_metric(db, *, maximum=5, scale_version=1):
    definition = create_definition_draft(
        db, focus_definition(maximum=maximum), actor="test", authorized=True
    )
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    metric_version = register_metric_definition(
        db, focus_metric(maximum=maximum, scale_version=scale_version), authorized=True
    )
    bind_event_field(
        db,
        event_version.id,
        "user.focus_session.focus",
        metric_version.id,
        authorized=True,
    )
    return definition, event_version, metric_version


def test_manual_events_at_same_time_keep_distinct_projection_facts(db):
    _, _, metric_version = activate_focus_metric(db)
    first = create_custom_event(db, entry(4), actor="test")
    second = create_custom_event(db, entry(3, distractions=2), actor="test")
    rows = db.scalars(select(MetricObservation).order_by(MetricObservation.source_entry_id)).all()

    assert len(rows) == 2
    assert {row.source_entry_id for row in rows} == {first.id, second.id}
    assert len({row.id for row in rows}) == 2
    assert all(row.metric_definition_version_id == metric_version.id for row in rows)
    result = aggregate_metric(db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1))
    assert result["value"] == {"3.0": 1, "4.0": 1}
    with pytest.raises(ValueError, match="not allowed"):
        aggregate_metric(
            db,
            "user.focus_session.focus",
            NOW,
            NOW + timedelta(hours=1),
            method="mean",
        )


def test_custom_projection_preview_reports_drift_without_writing(db):
    from garmin_ai.projection_audit import preview_custom_projection_drift

    activate_focus_metric(db)
    first = create_custom_event(db, entry(4), actor="test")
    second = create_custom_event(db, entry(3, start=NOW + timedelta(hours=1)), actor="test")
    rows = db.scalars(select(MetricObservation).order_by(MetricObservation.source_entry_id)).all()
    first_row = next(row for row in rows if row.source_entry_id == first.id)
    second_row = next(row for row in rows if row.source_entry_id == second.id)
    first_row.valid = False
    second_row.value = 2
    db.flush()

    preview = preview_custom_projection_drift(db)

    assert preview["totals"] == {
        "events": 2,
        "expected": 2,
        "valid": 1,
        "missing": 1,
        "stale": 0,
        "mismatched": 1,
        "history_unknown": 0,
        "pending": 0,
    }
    assert preview["writes"] is False
    assert first_row.valid is False and second_row.value == 2
    assert len(db.scalars(select(MetricObservation)).all()) == 2
    assert preview_custom_projection_drift(db, limit=1)["next_cursor"] is not None


def test_custom_projection_preview_detects_quality_drift_and_skips_deleted_pending(db):
    from garmin_ai.projection_audit import preview_custom_projection_drift

    activate_focus_metric(db)
    confirmed = create_custom_event(db, entry(4), actor="test")
    pending = create_custom_event(
        db,
        entry(3, start=NOW + timedelta(hours=1), status="needs_confirmation"),
        actor="test",
    )
    observation = db.scalar(
        select(MetricObservation).where(MetricObservation.source_entry_id == confirmed.id)
    )
    observation.quality = "estimated"
    delete_event(db, pending.id, revision=pending.revision, actor="test")
    db.flush()

    preview = preview_custom_projection_drift(db)
    summaries = {row["event_id"]: row for row in preview["rows"]}
    assert summaries[str(confirmed.id)]["mismatched"] == 1
    assert summaries[str(pending.id)]["pending"] is False
    assert preview["totals"]["pending"] == 0


def test_pending_custom_fact_enters_aggregate_only_after_confirmation(db):
    activate_focus_metric(db)
    pending = create_custom_event(db, entry(4, status="needs_confirmation"), actor="test")
    cutoff_before = datetime.now(UTC)
    from garmin_ai.generic_analytics import AnalysisSpec, query_entries, query_observations

    def spec(operation, cutoff):
        return AnalysisSpec(
            operation=operation,
            metric_key="user.focus_session.focus" if operation == "query_observations" else None,
            definition_key="user.focus_session" if operation == "query_entries" else None,
            start=NOW,
            end=NOW + timedelta(hours=1),
            knowledge_cutoff=cutoff,
        )

    assert (
        aggregate_metric(db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1))["value"]
        is None
    )
    stored = db.scalar(
        select(MetricObservation).where(MetricObservation.source_entry_id == pending.id)
    )
    assert stored.quality == "observed"
    assert query_observations(db, spec("query_observations", cutoff_before))["rows"] == []
    assert query_entries(db, spec("query_entries", cutoff_before))["rows"][0]["status"] == (
        "needs_confirmation"
    )
    update_custom_event(db, pending.id, entry(4), revision=pending.revision, actor="test")
    current = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=datetime.now(UTC) + timedelta(minutes=1),
    )
    before = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=cutoff_before,
    )

    assert current["value"] == {"4.0": 1}
    assert before["value"] is None
    assert query_entries(db, spec("query_entries", cutoff_before))["rows"][0]["status"] == (
        "needs_confirmation"
    )
    assert (
        query_observations(
            db, spec("query_observations", datetime.now(UTC) + timedelta(minutes=1))
        )["rows"][0]["owner_confirmation"]
        == "confirmed"
    )
    assert query_observations(db, spec("query_observations", cutoff_before))["rows"] == []

    undo_last(db, actor="test")
    assert (
        aggregate_metric(db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1))["value"]
        is None
    )
    delete_event(db, pending.id, revision=pending.revision, actor="test")
    undo_last(db, actor="test")
    assert not pending.deleted and pending.status == "needs_confirmation"
    assert (
        aggregate_metric(db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1))["value"]
        is None
    )


def test_metric_versions_keep_ordinal_scales_separate(db):
    definition, _, version_one = activate_focus_metric(db)
    old = create_custom_event(db, entry(4), actor="test")
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        focus_definition(maximum=10),
        actor="test",
        authorized=True,
    )
    event_version_two = activate_definition(
        db, definition.id, proposed.revision, actor="test", authorized=True
    )
    version_two = register_metric_definition(
        db, focus_metric(maximum=10, scale_version=2), authorized=True
    )
    bind_event_field(
        db,
        event_version_two.id,
        "user.focus_session.focus",
        version_two.id,
        authorized=True,
    )
    new = create_custom_event(db, entry(8, start=NOW + timedelta(minutes=30)), actor="test")

    assert old.definition_version_id != new.definition_version_id
    assert version_one.id != version_two.id
    old_result = aggregate_metric(
        db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1), version=1
    )
    new_result = aggregate_metric(
        db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1), version=2
    )
    assert old_result["value"] == {"4.0": 1} and old_result["scale_version"] == 1
    assert new_result["value"] == {"8.0": 1} and new_result["scale_version"] == 2


def test_entry_correction_invalidates_old_projection_and_preserves_lineage(db):
    activate_focus_metric(db)
    event = create_custom_event(db, entry(2), actor="test")
    update_custom_event(db, event.id, entry(5), revision=event.revision, actor="test")
    rows = db.scalars(
        select(MetricObservation)
        .where(MetricObservation.source_entry_id == event.id)
        .order_by(MetricObservation.projection_version)
    ).all()

    assert [(row.value, row.valid, row.projection_version) for row in rows] == [
        (2, False, 1),
        (5, True, 2),
    ]
    assert {row.source_ref for row in rows} == {event.id}
    assert rows[1].recorded_at == event.recorded_at
    assert rows[0].invalidated_at == rows[1].ingested_at

    undo_last(db, actor="test")
    rows = db.scalars(
        select(MetricObservation)
        .where(MetricObservation.source_entry_id == event.id)
        .order_by(MetricObservation.projection_version)
    ).all()
    assert [(row.value, row.valid, row.projection_version) for row in rows] == [
        (2, False, 1),
        (5, False, 2),
        (2, True, 3),
    ]


def test_projection_validity_is_evaluated_at_knowledge_cutoff(db):
    activate_focus_metric(db)
    event = create_custom_event(db, entry(2), actor="test")
    cutoff = datetime.now(UTC)
    update_custom_event(db, event.id, entry(5), revision=event.revision, actor="test")

    as_known = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=cutoff,
    )
    current = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert as_known["value"] == {"2.0": 1}
    assert current["value"] == {"5.0": 1}


def test_deleted_projection_remains_visible_before_deletion_cutoff(db):
    activate_focus_metric(db)
    event = create_custom_event(db, entry(4), actor="test")
    cutoff = datetime.now(UTC)
    delete_event(db, event.id, revision=event.revision, actor="test")

    as_known = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=cutoff,
    )

    assert as_known["value"] == {"4.0": 1}


def test_removing_optional_field_invalidates_its_projection(db):
    spec = focus_definition()
    spec.payload_schema["required"] = ["focus"]
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    metric = register_metric_definition(
        db,
        MetricSpec(
            key="user.focus_session.distractions",
            labels={"en": "Distractions"},
            value_kind="increment",
            unit="count",
            dimension="count",
            aggregation="sum",
            allowed_methods={"sum"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="interval",
            minimum=0,
            maximum=1000,
        ),
        authorized=True,
    )
    bind_event_field(
        db,
        event_version.id,
        "user.focus_session.distractions",
        metric.id,
        authorized=True,
    )
    event = create_custom_event(db, entry(4, distractions=3), actor="test")

    update_custom_event(
        db,
        event.id,
        CustomEntryInput(
            definition_key="user.focus_session",
            start=NOW,
            end=NOW + timedelta(minutes=25),
            timezone="UTC",
            values={"focus": 4},
            units={"focus": "score_1-5"},
        ),
        revision=event.revision,
        actor="test",
    )

    observation = db.scalar(
        select(MetricObservation).where(MetricObservation.source_entry_id == event.id)
    )
    assert observation.value == 3
    assert observation.valid is False
    assert (
        aggregate_metric(
            db,
            "user.focus_session.distractions",
            NOW,
            NOW + timedelta(hours=1),
        )["observations"]
        == 0
    )


def test_increment_intervals_sum_while_sparse_ordinal_needs_no_coverage(db):
    steps = register_metric_definition(
        db,
        MetricSpec(
            key="user.walking.steps",
            labels={"en": "Steps"},
            value_kind="increment",
            unit="steps",
            dimension="count",
            aggregation="sum",
            allowed_methods={"sum"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="interval",
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    for index, value in enumerate((100, 250)):
        start = NOW + timedelta(minutes=index * 15)
        record_observation(
            db,
            steps,
            value,
            observed_at=start,
            effective_start=start,
            effective_end=start + timedelta(minutes=15),
            source_ref=uuid4(),
        )
    result = aggregate_metric(db, "user.walking.steps", NOW, NOW + timedelta(hours=1))
    assert result["value"] == 350
    assert result["coverage_ratio"] is None


def test_time_weighted_contract_fails_closed_on_sparse_coverage(db):
    heart_rate = register_metric_definition(
        db,
        MetricSpec(
            key="user.manual_heart_rate",
            labels={"en": "Heart rate"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean", "latest"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=0.8, max_gap_seconds=300),
            time_semantics="interval",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    record_observation(
        db,
        heart_rate,
        70,
        observed_at=NOW,
        effective_start=NOW,
        effective_end=NOW + timedelta(minutes=10),
        source_ref=uuid4(),
    )
    record_observation(
        db,
        heart_rate,
        72,
        observed_at=NOW,
        effective_start=NOW,
        effective_end=NOW + timedelta(minutes=10),
        source_ref=uuid4(),
    )
    result = aggregate_metric(db, "user.manual_heart_rate", NOW, NOW + timedelta(hours=1))
    assert result["coverage_ratio"] == pytest.approx(1 / 6)
    assert result["value"] is None


def test_time_weighted_query_includes_bounded_pre_window_sample(db):
    heart_rate = register_metric_definition(
        db,
        MetricSpec(
            key="user.left_hold_heart_rate",
            labels={"en": "Heart rate"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=1, max_gap_seconds=300),
            time_semantics="interval",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    record_observation(
        db,
        heart_rate,
        70,
        observed_at=NOW - timedelta(minutes=1),
        effective_start=NOW - timedelta(minutes=1),
        source_ref=uuid4(),
    )

    result = aggregate_metric(db, "user.left_hold_heart_rate", NOW, NOW + timedelta(minutes=4))

    assert result["observations"] == 1
    assert result["coverage_ratio"] == 1
    assert result["value"] == 70


def test_interval_observation_is_selected_by_effective_overlap(db):
    heart_rate = register_metric_definition(
        db,
        MetricSpec(
            key="user.overlap_heart_rate",
            labels={"en": "Overlap heart rate"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=1, max_gap_seconds=60),
            time_semantics="interval",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    record_observation(
        db,
        heart_rate,
        72,
        observed_at=NOW - timedelta(minutes=10),
        effective_start=NOW - timedelta(minutes=10),
        effective_end=NOW + timedelta(minutes=10),
        source_ref=uuid4(),
    )

    result = aggregate_metric(db, "user.overlap_heart_rate", NOW, NOW + timedelta(minutes=5))

    assert result["observations"] == 1
    assert result["coverage_ratio"] == 1
    assert result["value"] == 72


def test_time_weighted_contract_rejects_gap_above_policy(db):
    heart_rate = register_metric_definition(
        db,
        MetricSpec(
            key="user.gapped_heart_rate",
            labels={"en": "Gapped heart rate"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=0.8, max_gap_seconds=300),
            time_semantics="interval",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    for start, end in ((0, 27), (33, 60)):
        record_observation(
            db,
            heart_rate,
            70,
            observed_at=NOW + timedelta(minutes=start),
            effective_start=NOW + timedelta(minutes=start),
            effective_end=NOW + timedelta(minutes=end),
            source_ref=uuid4(),
        )

    result = aggregate_metric(db, "user.gapped_heart_rate", NOW, NOW + timedelta(hours=1))

    assert result["coverage_ratio"] == pytest.approx(0.9)
    assert result["value"] is None


def test_metric_query_honors_as_known_cutoff(db):
    _, _, version = activate_focus_metric(db)
    record_observation(
        db,
        version,
        4,
        observed_at=NOW,
        effective_end=NOW + timedelta(minutes=25),
        source_ref=uuid4(),
    )

    before_ingestion = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=NOW,
    )
    after_ingestion = aggregate_metric(
        db,
        "user.focus_session.focus",
        NOW,
        NOW + timedelta(hours=1),
        knowledge_cutoff=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert before_ingestion["observations"] == 0
    assert after_ingestion["value"] == {"4.0": 1}


def test_metric_query_collapses_repeated_source_snapshots_as_known(db):
    version = register_metric_definition(
        db,
        MetricSpec(
            key="user.snapshot_heart_rate",
            labels={"en": "Snapshot heart rate"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="point",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    first = record_observation(
        db,
        version,
        60,
        observed_at=NOW,
        source_ref=uuid4(),
        uploaded_at=NOW,
    )
    second = record_observation(
        db,
        version,
        90,
        observed_at=NOW,
        source_ref=uuid4(),
        uploaded_at=NOW + timedelta(minutes=1),
    )
    first.feature_version = second.feature_version = "pre-event-v1"
    first.ingested_at = NOW
    second.ingested_at = NOW + timedelta(minutes=1)
    db.flush()

    before = aggregate_metric(
        db,
        "user.snapshot_heart_rate",
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
        method="mean",
        knowledge_cutoff=NOW + timedelta(seconds=30),
    )
    after = aggregate_metric(
        db,
        "user.snapshot_heart_rate",
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
        method="mean",
        knowledge_cutoff=NOW + timedelta(minutes=2),
    )

    assert before["observations"] == after["observations"] == 1
    assert before["value"] == 60
    assert after["value"] == 90


def test_physical_interval_mean_is_duration_weighted(db):
    contract = register_metric_definition(
        db,
        MetricSpec(
            key="user.interval_pulse",
            labels={"en": "Interval pulse"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=0.8, max_gap_seconds=3600),
            time_semantics="interval",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    for minutes, duration, value in ((0, 10, 60), (10, 50, 120)):
        start = NOW + timedelta(minutes=minutes)
        record_observation(
            db,
            contract,
            value,
            observed_at=start,
            effective_start=start,
            effective_end=start + timedelta(minutes=duration),
            source_ref=uuid4(),
        )

    result = aggregate_metric(db, "user.interval_pulse", NOW, NOW + timedelta(hours=1))
    assert result["coverage_ratio"] == 1
    assert result["value"] == 110


def test_metric_versions_are_immutable_and_units_are_dimension_checked(db):
    _, _, version = activate_focus_metric(db)
    with pytest.raises(DatabaseError), db.begin_nested():
        version.maximum = 6
        db.flush()
    assert convert_unit(120, "minutes", "hours") == 2
    with pytest.raises(ValueError, match="incompatible"):
        convert_unit(1, "hours", "km")
    with pytest.raises(ValueError, match="Zero speed or pace"):
        convert_unit(0, "km/h", "s/km")
    with pytest.raises(ValueError, match="Zero speed or pace"):
        convert_unit(0, "s/km", "km/h")
    with pytest.raises(ValueError, match="negative values"):
        convert_unit(-1, "km/h", "s/km")


def test_event_field_mapping_rejects_semantic_mismatch(db):
    definition = create_definition_draft(db, focus_definition(), actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    physical = register_metric_definition(
        db,
        MetricSpec(
            key="user.focus_as_physical",
            labels={"en": "Focus as physical"},
            value_kind="physical_number",
            unit="score_1-5",
            dimension="ordinal",
            aggregation="latest",
            allowed_methods={"latest"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="interval",
            minimum=1,
            maximum=5,
        ),
        authorized=True,
    )

    with pytest.raises(ValueError, match="value kinds"):
        bind_event_field(
            db,
            event_version.id,
            "user.focus_session.focus",
            physical.id,
            authorized=True,
        )


def test_event_field_mapping_rejects_wider_numeric_domain(db):
    _, event_version, _ = activate_focus_metric(db)
    narrow = register_metric_definition(
        db,
        MetricSpec(
            key="user.focus_session.narrow",
            labels={"en": "Narrow focus"},
            value_kind="ordinal",
            unit="score_1-5",
            dimension="ordinal",
            scale_id="user.focus",
            scale_version=1,
            aggregation="latest",
            allowed_methods={"latest"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="point",
            minimum=2,
            maximum=4,
        ),
        authorized=True,
    )

    with pytest.raises(ValueError, match="domain"):
        bind_event_field(
            db,
            event_version.id,
            "user.focus_session.focus",
            narrow.id,
            projection_version=2,
            authorized=True,
        )


def test_event_field_mapping_rejects_schema_semantic_mismatch(db):
    spec = focus_definition()
    spec.payload_schema["properties"]["focus"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 20,
    }
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    ordinal = register_metric_definition(db, focus_metric(), authorized=True)

    with pytest.raises(ValueError, match="schema type"):
        bind_event_field(
            db,
            event_version.id,
            "user.focus_session.focus",
            ordinal.id,
            authorized=True,
        )


def test_latest_mapping_version_is_the_only_active_projection(db):
    _, event_version, metric_one = activate_focus_metric(db)
    metric_two = register_metric_definition(
        db, focus_metric(maximum=5, scale_version=2), authorized=True
    )
    bind_event_field(
        db,
        event_version.id,
        "user.focus_session.focus",
        metric_two.id,
        projection_version=2,
        authorized=True,
    )

    event = create_custom_event(db, entry(4), actor="test")
    observation = db.scalar(
        select(MetricObservation).where(
            MetricObservation.source_entry_id == event.id,
            MetricObservation.valid.is_(True),
        )
    )

    assert metric_one.id != metric_two.id
    assert observation.metric_definition_version_id == metric_two.id


def test_system_measurements_backfill_to_explicit_metric_versions(db):
    row = Measurement(
        ts=NOW,
        metric="heart_rate_bpm",
        source="synthetic",
        local_date=NOW.date(),
        value=70,
        unit="bpm",
        source_ref=uuid4(),
        quality="observed",
        details={},
    )
    db.add(row)
    db.flush()

    versions = ensure_system_metric_definitions(db, backfill=True)
    db.refresh(row)

    assert row.metric_definition_version_id == versions["heart_rate_bpm"].id
    assert versions["stress_score"].coverage_policy["kind"] == "time_weighted"

    result = aggregate_metric(
        db,
        "system.heart_rate_bpm",
        NOW,
        NOW + timedelta(minutes=4),
    )
    assert result["observations"] == 1
    assert result["value"] == 70


def test_measurement_knowledge_cutoff_uses_ingestion_time_not_observation_time(db):
    version = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    row = Measurement(
        ts=NOW,
        metric="heart_rate_bpm",
        source="synthetic",
        local_date=NOW.date(),
        value=70,
        unit="bpm",
        metric_definition_version_id=version.id,
        source_ref=uuid4(),
        quality="observed",
        details={},
        ingested_at=NOW + timedelta(days=1),
    )
    db.add(row)
    db.flush()

    before = aggregate_metric(
        db,
        "system.heart_rate_bpm",
        NOW,
        NOW + timedelta(minutes=1),
        knowledge_cutoff=NOW + timedelta(hours=1),
    )
    after = aggregate_metric(
        db,
        "system.heart_rate_bpm",
        NOW,
        NOW + timedelta(minutes=1),
        knowledge_cutoff=NOW + timedelta(days=2),
    )

    assert before["observations"] == 0 and before["value"] is None
    assert after["observations"] == 1 and after["value"] == 70
    assert after["latest_known_at"] == row.ingested_at.isoformat()


def test_nominal_category_domain_change_creates_new_metric_version(db):
    def mood(domain):
        return MetricSpec(
            key="user.mood.label",
            labels={"en": "Mood"},
            value_kind="nominal",
            unit=None,
            dimension="category",
            aggregation="counts",
            allowed_methods={"counts", "latest", "mode"},
            category_domain=domain,
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="point",
        )

    first = register_metric_definition(db, mood(["good", "bad"]), authorized=True)
    second = register_metric_definition(db, mood(["good", "neutral"]), authorized=True)

    assert first.version == 1 and first.category_domain == ["good", "bad"]
    assert second.version == 2 and second.category_domain == ["good", "neutral"]
    assert first.schema_hash != second.schema_hash


def test_nested_schema_reference_mapping_and_null_projection(db):
    spec = focus_definition()
    spec.payload_schema["$defs"] = {"score": {"type": "integer", "minimum": 1, "maximum": 5}}
    spec.payload_schema["properties"]["focus"] = {
        "anyOf": [{"$ref": "#/$defs/score"}, {"type": "null"}]
    }
    spec.payload_schema["required"].remove("focus")
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    metric = register_metric_definition(db, focus_metric(), authorized=True)
    bind_event_field(
        db,
        event_version.id,
        "user.focus_session.focus",
        metric.id,
        authorized=True,
    )

    event = create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.focus_session",
            start=NOW,
            end=NOW + timedelta(minutes=5),
            timezone="UTC",
            values={"focus": None, "distractions": 0},
            units={"focus": "score_1-5", "distractions": "count"},
        ),
        actor="test",
    )

    assert (
        db.scalar(select(MetricObservation).where(MetricObservation.source_entry_id == event.id))
        is None
    )


def test_system_weighted_gauges_use_bounded_left_hold_intervals(db):
    heart_rate = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    for minutes, value in ((0, 70), (4, 80), (8, 90)):
        at = NOW + timedelta(minutes=minutes)
        record_observation(
            db,
            heart_rate,
            value,
            observed_at=at,
            effective_start=at,
            source_ref=uuid4(),
        )

    result = aggregate_metric(
        db,
        "system.heart_rate_bpm",
        NOW,
        NOW + timedelta(minutes=10),
    )

    assert heart_rate.time_semantics == "interval"
    assert result["coverage_ratio"] == 1
    assert result["value"] == pytest.approx(78)


def test_system_event_writes_and_updates_project_bound_fields(db):
    event_version = ensure_system_definition(db, "migraine")
    metric = register_metric_definition(
        db,
        MetricSpec(
            key="user.migraine.severity",
            labels={"en": "Migraine severity"},
            value_kind="ordinal",
            unit="score_1-10",
            dimension="ordinal",
            scale_id="user.migraine.severity",
            scale_version=1,
            aggregation="latest",
            allowed_methods={"latest", "median", "distribution"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="point",
            minimum=0,
            maximum=10,
        ),
        authorized=True,
    )
    bind_event_field(
        db,
        event_version.id,
        "system.migraine.severity",
        metric.id,
        authorized=True,
    )

    event = create_event(
        db,
        EventInput(start=NOW, payload={"type": "migraine", "severity": 3}),
        actor="test",
    )
    update_event(
        db,
        event.id,
        EventInput(start=NOW, payload={"type": "migraine", "severity": 5}),
        revision=event.revision,
        actor="test",
    )
    rows = db.scalars(
        select(MetricObservation)
        .where(MetricObservation.source_entry_id == event.id)
        .order_by(MetricObservation.projection_version)
    ).all()

    assert [(row.value, row.valid) for row in rows] == [(3, False), (5, True)]

    first_invalidated_at = rows[0].invalidated_at
    delete_event(db, event.id, revision=event.revision, actor="test")
    db.refresh(rows[0])
    assert rows[0].invalidated_at == first_invalidated_at


def test_single_counter_observation_has_unknown_delta(db):
    counter = register_metric_definition(
        db,
        MetricSpec(
            key="user.counter",
            labels={"en": "Counter"},
            value_kind="cumulative_counter",
            unit="count",
            dimension="count",
            aggregation="delta",
            allowed_methods={"delta", "latest"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="point",
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    record_observation(db, counter, 10, observed_at=NOW, source_ref=uuid4())

    result = aggregate_metric(db, "user.counter", NOW, NOW + timedelta(hours=1))

    assert result["observations"] == 1
    assert result["value"] is None


@pytest.mark.parametrize("time_semantics", ["point", "interval"])
def test_counter_delta_uses_pre_window_sample_without_counting_it(db, time_semantics):
    counter = register_metric_definition(
        db,
        MetricSpec(
            key="user.counter.baseline",
            labels={"en": "Counter baseline"},
            value_kind="cumulative_counter",
            unit="count",
            dimension="count",
            aggregation="delta",
            allowed_methods={"delta", "latest"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics=time_semantics,
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    record_observation(db, counter, 100, observed_at=NOW - timedelta(minutes=1), source_ref=uuid4())
    record_observation(
        db, counter, 150, observed_at=NOW + timedelta(minutes=30), source_ref=uuid4()
    )

    result = aggregate_metric(db, "user.counter.baseline", NOW, NOW + timedelta(hours=1))

    assert result["observations"] == 1
    assert result["value"] == 50


def test_counter_delta_uses_latest_sample_before_window(db):
    counter = register_metric_definition(
        db,
        MetricSpec(
            key="user.counter_boundary",
            labels={"en": "Counter boundary"},
            value_kind="cumulative_counter",
            unit="count",
            dimension="count",
            aggregation="delta",
            allowed_methods={"delta", "latest"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="interval",
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    for minutes, value in ((-60, 4), (-1, 10), (1, 15)):
        record_observation(
            db,
            counter,
            value,
            observed_at=NOW + timedelta(minutes=minutes),
            source_ref=uuid4(),
        )

    result = aggregate_metric(db, "user.counter_boundary", NOW, NOW + timedelta(hours=1))

    assert result["value"] == 5
    assert result["observations"] == 1


def test_counter_delta_uses_measurement_revision_before_window(db):
    counter = register_metric_definition(
        db,
        MetricSpec(
            key="user.measurement_counter",
            labels={"en": "Measurement counter"},
            value_kind="cumulative_counter",
            unit="count",
            dimension="count",
            aggregation="delta",
            allowed_methods={"delta", "latest"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="point",
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    for minutes, value in ((-1, 100), (30, 150)):
        stamp = NOW + timedelta(minutes=minutes)
        db.add(
            MeasurementRevision(
                ts=stamp,
                metric="measurement_counter",
                source="synthetic",
                local_date=stamp.date(),
                value=value,
                unit="count",
                metric_definition_version_id=counter.id,
                source_ref=uuid4(),
                quality="observed",
                details={},
                ingested_at=stamp,
            )
        )
    db.flush()

    result = aggregate_metric(
        db,
        "user.measurement_counter",
        NOW,
        NOW + timedelta(hours=1),
        source="measurement:synthetic",
        knowledge_cutoff=NOW + timedelta(hours=2),
    )

    assert result["observations"] == 1
    assert result["value"] == 50


def test_measurement_revision_source_filter_is_applied_before_limit(db):
    version = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    for minute, source in ((0, "other"), (1, "selected")):
        stamp = NOW + timedelta(minutes=minute)
        db.add(
            MeasurementRevision(
                ts=stamp,
                metric="heart_rate_bpm",
                source=source,
                local_date=stamp.date(),
                value=70 + minute,
                unit="bpm",
                metric_definition_version_id=version.id,
                source_ref=uuid4(),
                quality="observed",
                details={},
                ingested_at=stamp,
            )
        )
    db.flush()

    rows = measurement_rows_as_of(
        db,
        version.id,
        NOW,
        NOW + timedelta(hours=1),
        NOW + timedelta(hours=2),
        source="selected",
        limit=1,
    )

    assert len(rows) == 1
    assert rows[0].source == "selected"


def test_aggregate_requires_source_selection_for_overlapping_providers(db):
    counter = register_metric_definition(
        db,
        MetricSpec(
            key="user.provider.steps",
            labels={"en": "Provider steps"},
            value_kind="increment",
            unit="steps",
            dimension="count",
            aggregation="sum",
            allowed_methods={"sum"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="point",
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    first = record_observation(db, counter, 100, observed_at=NOW, source_ref=uuid4())
    second = record_observation(db, counter, 120, observed_at=NOW, source_ref=uuid4())
    first.account, first.device = "provider-a", "watch"
    second.account, second.device = "provider-b", "watch"
    db.flush()

    with pytest.raises(ValueError, match="Multiple metric sources"):
        aggregate_metric(db, "user.provider.steps", NOW, NOW + timedelta(hours=1))
    selected = aggregate_metric(
        db,
        "user.provider.steps",
        NOW,
        NOW + timedelta(hours=1),
        source='observation:["provider-a","watch"]',
    )
    assert selected["value"] == 100
    assert selected["observations"] == 1


def test_inferred_counter_source_scopes_pre_window_delta_sample(db):
    counter = register_metric_definition(
        db,
        MetricSpec(
            key="user.provider.counter",
            labels={"en": "Provider counter"},
            value_kind="cumulative_counter",
            unit="count",
            dimension="count",
            aggregation="delta",
            allowed_methods={"delta", "latest"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="point",
            minimum=0,
            maximum=1_000_000,
        ),
        authorized=True,
    )
    prior_a = record_observation(
        db, counter, 100, observed_at=NOW - timedelta(minutes=2), source_ref=uuid4()
    )
    prior_b = record_observation(
        db, counter, 1_000, observed_at=NOW - timedelta(minutes=1), source_ref=uuid4()
    )
    current_a = record_observation(
        db, counter, 150, observed_at=NOW + timedelta(minutes=1), source_ref=uuid4()
    )
    prior_a.account = current_a.account = "provider-a"
    prior_a.device = current_a.device = "watch"
    prior_b.account = "provider-b"
    prior_b.device = "watch"
    db.flush()

    result = aggregate_metric(db, "user.provider.counter", NOW, NOW + timedelta(hours=1))

    assert result["source"] == 'observation:["provider-a","watch"]'
    assert result["value"] == 50


def test_incomplete_interval_coverage_cannot_pass_full_coverage_gate(db):
    version = register_metric_definition(
        db,
        MetricSpec(
            key="user.partial.coverage",
            labels={"en": "Partial coverage"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="mean",
            allowed_methods={"mean"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=0.8, max_gap_seconds=3600),
            time_semantics="interval",
            minimum=1,
            maximum=300,
        ),
        authorized=True,
    )
    record_observation(
        db,
        version,
        70,
        observed_at=NOW,
        effective_start=NOW,
        effective_end=NOW + timedelta(hours=1),
        source_ref=uuid4(),
        coverage=0.1,
    )

    result = aggregate_metric(db, "user.partial.coverage", NOW, NOW + timedelta(hours=1))

    assert result["coverage_ratio"] <= 0.1
    assert result["value"] is None
