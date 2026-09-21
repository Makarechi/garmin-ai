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
    record_observation,
    register_metric_definition,
)
from garmin_ai.models import Measurement, MetricObservation, SourcePayload

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def test_api_readiness_skips_metric_bootstrap_after_initialization(db, db_engine, monkeypatch):
    from fastapi.testclient import TestClient

    import garmin_ai.metric_definitions as registry
    from garmin_ai.api import create_app
    from garmin_ai.config import Settings

    db.commit()
    app = create_app(Settings(), db_engine)

    def unexpected_bootstrap(*args, **kwargs):
        raise AssertionError("Metric bootstrap must not run for an initialized request")

    monkeypatch.setattr(registry, "ensure_system_metric_definitions", unexpected_bootstrap)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200


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
    assert rows[1].recorded_at == event.recorded_at

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


def test_time_weighted_min_ignores_expired_predecessors(db):
    version = register_metric_definition(
        db,
        MetricSpec(
            key="user.left_hold_min",
            labels={"en": "Minimum heart rate"},
            value_kind="physical_number",
            unit="bpm",
            dimension="frequency",
            aggregation="min",
            allowed_methods={"min"},
            coverage=CoveragePolicy(kind="time_weighted", minimum_ratio=1, max_gap_seconds=300),
            time_semantics="interval",
            minimum=0,
            maximum=300,
        ),
        authorized=True,
    )
    for minutes, value in ((4, 10), (3, 80)):
        record_observation(
            db,
            version,
            value,
            observed_at=NOW - timedelta(minutes=minutes),
            effective_start=NOW - timedelta(minutes=minutes),
            source_ref=uuid4(),
        )

    result = aggregate_metric(db, "user.left_hold_min", NOW, NOW + timedelta(minutes=2))
    assert result["value"] == 80
    assert result["observations"] == 1


def test_interval_total_is_not_summed_across_partial_windows(db):
    version = register_metric_definition(
        db,
        MetricSpec(
            key="user.interval_total",
            labels={"en": "Interval total"},
            value_kind="interval_total",
            unit="minutes",
            dimension="duration",
            aggregation="sum",
            allowed_methods={"sum"},
            coverage=CoveragePolicy(kind="all_values"),
            time_semantics="interval",
            minimum=0,
            maximum=1000,
        ),
        authorized=True,
    )
    record_observation(
        db,
        version,
        60,
        observed_at=NOW,
        effective_start=NOW,
        effective_end=NOW + timedelta(hours=1),
        source_ref=uuid4(),
    )
    partial = aggregate_metric(db, "user.interval_total", NOW, NOW + timedelta(minutes=30))
    whole = aggregate_metric(db, "user.interval_total", NOW, NOW + timedelta(hours=1))
    assert partial["observations"] == 0 and partial["value"] is None
    assert whole["value"] == 60


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


def test_new_mapping_reprojects_existing_events_and_records_activation_time(db):
    _, event_version, metric_one = activate_focus_metric(db)
    event = create_custom_event(db, entry(4), actor="test")
    original = db.scalar(
        select(MetricObservation).where(MetricObservation.source_entry_id == event.id)
    )
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
    db.refresh(original)
    current = db.scalar(
        select(MetricObservation).where(
            MetricObservation.source_entry_id == event.id, MetricObservation.valid.is_(True)
        )
    )
    assert not original.valid
    assert current.metric_definition_version_id == metric_two.id
    assert current.recorded_at >= original.recorded_at


def test_mapping_rejects_schema_values_outside_metric_bounds(db):
    spec = focus_definition()
    spec.payload_schema["properties"]["focus"]["maximum"] = 10
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    metric = register_metric_definition(db, focus_metric(), authorized=True)
    with pytest.raises(ValueError, match="range exceeds"):
        bind_event_field(
            db,
            event_version.id,
            "user.focus_session.focus",
            metric.id,
            authorized=True,
        )


def test_nominal_mapping_rejects_schema_that_allows_empty_strings(db):
    spec = focus_definition()
    spec.payload_schema["properties"]["distractions"] = {"type": "string", "maxLength": 100}
    spec.fields["distractions"].semantic = "nominal"
    spec.fields["distractions"].unit = None
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    metric = register_metric_definition(
        db,
        MetricSpec(
            key="user.focus_session.distractions",
            labels={"en": "Distractions"},
            value_kind="nominal",
            dimension="category",
            aggregation="latest",
            allowed_methods={"latest", "counts"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="point",
        ),
        authorized=True,
    )
    with pytest.raises(ValueError, match="Nominal event field"):
        bind_event_field(
            db,
            event_version.id,
            "user.focus_session.distractions",
            metric.id,
            authorized=True,
        )


def test_interval_start_before_window_and_partial_total_boundary(db):
    versions = ensure_system_metric_definitions(db)
    record_observation(
        db,
        versions["sleep_score"],
        88,
        observed_at=NOW + timedelta(minutes=30),
        effective_start=NOW,
        source_ref=uuid4(),
    )
    sleep = aggregate_metric(
        db, "system.sleep_score", NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)
    )
    assert sleep["value"] == 88

    record_observation(
        db,
        versions["steps_bucket"],
        100,
        observed_at=NOW + timedelta(minutes=15),
        effective_start=NOW,
        effective_end=NOW + timedelta(minutes=15),
        source_ref=uuid4(),
    )
    partial = aggregate_metric(
        db, "system.steps_bucket", NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)
    )
    complete = aggregate_metric(db, "system.steps_bucket", NOW, NOW + timedelta(minutes=15))
    assert partial["value"] is None
    assert complete["value"] == 100


def test_calendar_period_is_rejected_until_window_selection_is_supported():
    spec = focus_metric().model_dump()
    spec["time_semantics"] = "calendar_period"
    with pytest.raises(ValueError, match="Calendar-period"):
        MetricSpec.model_validate(spec)


def test_nonqueryable_event_projections_are_excluded_from_aggregation(db):
    spec = focus_definition()
    spec.allowed_operations = {"create", "update", "delete"}
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    event_version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    metric = register_metric_definition(db, focus_metric(), authorized=True)
    bind_event_field(db, event_version.id, "user.focus_session.focus", metric.id, authorized=True)
    event = create_custom_event(db, entry(4), actor="test")
    assert db.scalar(select(MetricObservation).where(MetricObservation.source_entry_id == event.id))
    result = aggregate_metric(db, "user.focus_session.focus", NOW, NOW + timedelta(hours=1))
    assert result["value"] is None
    assert result["source_refs"] == []


def test_corrected_projection_uses_event_update_time(db):
    activate_focus_metric(db)
    event = create_custom_event(db, entry(4), actor="test")
    db.commit()
    updated = update_custom_event(db, event.id, entry(5), revision=event.revision, actor="test")
    current = db.scalar(
        select(MetricObservation).where(
            MetricObservation.source_entry_id == event.id, MetricObservation.valid.is_(True)
        )
    )
    assert current.recorded_at == updated.updated_at


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


def test_measurement_knowledge_cutoff_uses_source_fetch_time(db):
    payload = SourcePayload(
        source="synthetic",
        endpoint="daily",
        source_key="sample",
        payload_hash="synthetic-hash",
        payload={},
        archive_key="synthetic",
        fetched_at=NOW + timedelta(hours=3),
        status="projected",
    )
    db.add(payload)
    db.flush()
    db.add(
        Measurement(
            ts=NOW,
            metric="heart_rate_bpm",
            source="synthetic",
            local_date=NOW.date(),
            value=70,
            unit="bpm",
            source_ref=payload.id,
            quality="observed",
            details={},
        )
    )
    ensure_system_metric_definitions(db, backfill=True)

    before = aggregate_metric(
        db,
        "system.heart_rate_bpm",
        NOW,
        NOW + timedelta(minutes=2),
        knowledge_cutoff=NOW + timedelta(hours=2),
    )
    after = aggregate_metric(
        db,
        "system.heart_rate_bpm",
        NOW,
        NOW + timedelta(minutes=2),
        knowledge_cutoff=NOW + timedelta(hours=4),
    )
    assert before["observations"] == 0
    assert after["value"] == 70
    assert after["latest_known_at"] == payload.fetched_at.isoformat()


@pytest.mark.parametrize("source,target", [("m/s", "s/km"), ("s/km", "m/s")])
def test_reciprocal_unit_conversion_rejects_zero(source, target):
    with pytest.raises(ValueError, match="positive"):
        convert_unit(0, source, target)


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
