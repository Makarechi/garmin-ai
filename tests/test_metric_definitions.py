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
    propose_definition_revision,
    update_custom_event,
)
from garmin_ai.events import undo_last
from garmin_ai.metric_definitions import (
    CoveragePolicy,
    MetricSpec,
    aggregate_metric,
    bind_event_field,
    convert_unit,
    ensure_system_metric_definitions,
    record_observation,
    register_metric_definition,
)
from garmin_ai.models import Measurement, MetricObservation

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


def entry(value, *, start=NOW, distractions=0):
    return CustomEntryInput(
        definition_key="user.focus_session",
        start=start,
        end=start + timedelta(minutes=25),
        timezone="UTC",
        values={"focus": value, "distractions": distractions},
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
