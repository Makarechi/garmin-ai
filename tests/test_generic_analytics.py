from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select

from garmin_ai.definitions import (
    CustomEntryInput,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    update_custom_event,
)
from garmin_ai.events import EventInput, create_event, update_event
from garmin_ai.generic_analytics import (
    AnalysisSpec,
    DimensionedValue,
    evaluate_formula,
    evidence_is_stale,
    execute_analysis,
)
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import (
    Audit,
    Event,
    EventDefinitionVersion,
    Measurement,
    MeasurementRevision,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    SourcePayload,
)
from garmin_ai.reconciliation import Replacement, replace_interval
from garmin_ai.scenario_packs import ensure_scenario_packs
from garmin_ai.tools import call_tool
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    action_for_event,
    confirm_tracker,
    definition_spec,
    form_for_action,
    preview_tracker,
    submit_form,
)

NOW = datetime(2026, 9, 20, 18, tzinfo=UTC)
CUTOFF = datetime.now(UTC) + timedelta(days=1)


def install(db):
    draft = TrackerSetupDraft(
        key="focus",
        name="Focus",
        locale="en",
        topology="point",
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
        shortcut="Log focus",
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    action = created["action"]
    for index, value in enumerate((1, 5, 5)):
        form = form_for_action(db, action["id"])
        submit_form(
            db,
            action["id"],
            FormSubmission(
                action_id=action["id"],
                schema_hash=form.schema_hash,
                start=NOW + timedelta(hours=index),
                timezone="UTC",
                values={"quality": value},
                units={"quality": "score_1-5"},
            ),
            actor="test",
            idempotency_key=f"focus:{index}",
        )
    db.flush()
    return db.scalar(select(MetricDefinition).where(MetricDefinition.key.like("user.%")))


def spec(metric, operation="aggregate_metric", **changes):
    values = dict(
        operation=operation,
        metric_key=metric.key,
        start=NOW - timedelta(minutes=1),
        end=NOW + timedelta(hours=4),
        method="distribution",
        knowledge_cutoff=CUTOFF,
    )
    values.update(changes)
    return AnalysisSpec(**values)


def test_ordinal_history_and_distribution_preserve_versioned_scale(db):
    metric = install(db)
    result = execute_analysis(db, spec(metric))
    rows = execute_analysis(db, spec(metric, "query_observations", method=None))

    assert result["value"] == {"1.0": 1, "5.0": 2}
    assert result["scale_id"] == "user.focus.quality"
    assert len(rows["rows"]) == 3
    assert rows["scale_id"] == result["scale_id"]


def test_unknown_metric_version_has_controlled_error(db):
    metric = install(db)
    for operation in ("aggregate_metric", "query_observations", "query_completeness"):
        with pytest.raises(LookupError, match="Metric version not found"):
            execute_analysis(db, spec(metric, operation, metric_version=999))


def test_sparse_aggregate_does_not_claim_reporting_completeness(db):
    metric = install(db)
    result = execute_analysis(db, spec(metric, "query_completeness"))

    assert result["aggregate_available"] is True
    assert result["reporting_completeness"] == "unknown"
    assert result["complete"] is None
    assert result["coverage_ratio"] is None


def test_generic_source_selector_separates_event_and_measurement_facts(db):
    metric = install(db)
    version = db.scalar(
        select(MetricDefinitionVersion).where(
            MetricDefinitionVersion.definition_id == metric.id,
            MetricDefinitionVersion.version == metric.current_version,
        )
    )
    db.add(
        Measurement(
            ts=NOW,
            metric=metric.key,
            source="synthetic-sensor",
            local_date=NOW.date(),
            value=3,
            unit=version.unit,
            metric_definition_version_id=version.id,
            source_ref=None,
            quality="observed",
            details={},
            ingested_at=NOW + timedelta(minutes=1),
        )
    )
    db.flush()

    with pytest.raises(ValueError, match="Multiple metric sources"):
        execute_analysis(db, spec(metric))
    events = execute_analysis(db, spec(metric, source="event"))
    sensor = execute_analysis(db, spec(metric, source="measurement:synthetic-sensor"))

    assert events["value"] == {"1.0": 1, "5.0": 2}
    assert sensor["value"] == {"3.0": 1}
    event_rows = execute_analysis(
        db, spec(metric, "query_observations", method=None, source="event")
    )["rows"]
    sensor_rows = execute_analysis(
        db, spec(metric, "query_observations", method=None, source="measurement:synthetic-sensor")
    )["rows"]
    assert len(event_rows) == 3
    assert {row["metric_source"] for row in event_rows} == {"event"}
    assert {row["source"] for row in event_rows} == set(db.scalars(select(Event.source)))
    assert len(sensor_rows) == 1
    assert sensor_rows[0]["metric_source"] == "measurement:synthetic-sensor"
    assert sensor_rows[0]["source"] is None


def test_generic_source_selector_is_bounded_and_metric_only(db):
    metric = install(db)
    assert spec(metric, source='observation:["provider-a", "watch"]').source == (
        'observation:["provider-a","watch"]'
    )
    with pytest.raises(ValidationError, match="Invalid metric source"):
        spec(metric, source="measurement:")
    with pytest.raises(ValidationError, match="string_too_long"):
        spec(metric, source="measurement:" + "x" * 200)
    with pytest.raises(ValidationError, match="Metric source is only supported"):
        AnalysisSpec(
            operation="query_entries",
            definition_key="user.focus",
            start=NOW,
            end=NOW + timedelta(hours=1),
            knowledge_cutoff=CUTOFF,
            source="event",
        )


def test_observation_query_includes_measurement_backed_system_metrics(db):
    version = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    db.add(
        Measurement(
            ts=NOW,
            metric="heart_rate_bpm",
            source="synthetic",
            local_date=NOW.date(),
            value=72,
            unit="bpm",
            metric_definition_version_id=version.id,
            source_ref=None,
            quality="observed",
            details={},
            ingested_at=NOW + timedelta(minutes=1),
        )
    )
    db.flush()

    result = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_observations",
            metric_key="system.heart_rate_bpm",
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(minutes=1),
            knowledge_cutoff=NOW + timedelta(minutes=2),
        ),
    )

    assert len(result["rows"]) == 1
    assert result["rows"][0]["value"] == 72
    assert result["rows"][0]["id"].startswith("measurement:heart_rate_bpm:")
    assert result["rows"][0]["source"] is None


def test_measurement_queries_restore_value_known_before_a_corrected_refetch(db):
    version = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    first_ref, corrected_ref = uuid4(), uuid4()
    values = dict(
        ts=NOW,
        metric="heart_rate_bpm",
        source="synthetic",
        local_date=NOW.date(),
        unit="bpm",
        metric_definition_version_id=version.id,
        quality="observed",
        details={},
    )
    db.add_all(
        [
            Measurement(
                **values,
                value=80,
                source_ref=corrected_ref,
                ingested_at=NOW + timedelta(hours=2),
            ),
            MeasurementRevision(
                **values,
                value=70,
                source_ref=first_ref,
                ingested_at=NOW + timedelta(minutes=1),
            ),
            MeasurementRevision(
                **values,
                value=80,
                source_ref=corrected_ref,
                ingested_at=NOW + timedelta(hours=2),
            ),
        ]
    )
    db.flush()

    request = AnalysisSpec(
        operation="query_observations",
        metric_key="system.heart_rate_bpm",
        start=NOW - timedelta(minutes=1),
        end=NOW + timedelta(minutes=1),
        knowledge_cutoff=NOW + timedelta(minutes=30),
    )

    assert execute_analysis(db, request)["rows"][0]["value"] == 70
    assert (
        execute_analysis(
            db,
            request.model_copy(update={"knowledge_cutoff": NOW + timedelta(hours=3)}),
        )["rows"][0]["value"]
        == 80
    )


def test_measurement_queries_apply_authoritative_deletion_at_its_knowledge_time(db):
    version = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    first_ref, deletion_ref = uuid4(), uuid4()
    first_fetch = NOW + timedelta(minutes=1)
    deletion_fetch = NOW + timedelta(hours=2)
    for identity, fetched_at, payload_hash in (
        (first_ref, first_fetch, "first"),
        (deletion_ref, deletion_fetch, "deletion"),
    ):
        db.add(
            SourcePayload(
                id=identity,
                source="synthetic",
                endpoint="heart_rate",
                source_key="2026-09-20",
                payload_hash=payload_hash,
                payload=[],
                archive_key=f"synthetic/{payload_hash}.json",
                fetched_at=fetched_at,
            )
        )
    values = dict(
        ts=NOW,
        metric="heart_rate_bpm",
        source="synthetic",
        local_date=NOW.date(),
        value=70,
        unit="bpm",
        metric_definition_version_id=version.id,
        source_ref=first_ref,
        quality="observed",
        details={},
        ingested_at=first_fetch,
    )
    db.add_all([Measurement(**values), MeasurementRevision(**values)])
    db.flush()
    replace_interval(
        db,
        "synthetic",
        "heart_rate",
        "2026-09-20",
        Replacement(
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(minutes=1),
            metrics=("heart_rate_bpm",),
            evidence="authoritative empty interval",
        ),
    )
    request = AnalysisSpec(
        operation="query_observations",
        metric_key="system.heart_rate_bpm",
        start=NOW - timedelta(minutes=1),
        end=NOW + timedelta(minutes=1),
        knowledge_cutoff=NOW + timedelta(minutes=30),
    )

    assert execute_analysis(db, request)["rows"][0]["value"] == 70
    assert (
        execute_analysis(
            db,
            request.model_copy(update={"knowledge_cutoff": NOW + timedelta(hours=3)}),
        )["rows"]
        == []
    )


def test_measurement_aggregate_evidence_becomes_stale_after_authoritative_refetch(db):
    version = ensure_system_metric_definitions(db)["heart_rate_bpm"]
    values = dict(
        ts=NOW,
        metric="heart_rate_bpm",
        source="synthetic",
        local_date=NOW.date(),
        unit="bpm",
        metric_definition_version_id=version.id,
        quality="observed",
        details={},
    )
    db.add(
        MeasurementRevision(
            **values,
            value=70,
            source_ref=uuid4(),
            ingested_at=NOW + timedelta(minutes=1),
        )
    )
    db.flush()
    request = AnalysisSpec(
        operation="aggregate_metric",
        metric_key="system.heart_rate_bpm",
        start=NOW - timedelta(minutes=1),
        end=NOW + timedelta(minutes=1),
        method="mean",
        knowledge_cutoff=NOW + timedelta(minutes=30),
    )

    result = execute_analysis(db, request)

    assert result["input_revisions"]
    assert not evidence_is_stale(db, result)
    db.add(
        MeasurementRevision(
            **values,
            value=80,
            source_ref=uuid4(),
            ingested_at=NOW + timedelta(hours=2),
        )
    )
    db.flush()
    assert evidence_is_stale(db, result)

    corrected = execute_analysis(
        db,
        request.model_copy(update={"knowledge_cutoff": NOW + timedelta(hours=3)}),
    )
    assert not evidence_is_stale(db, corrected)
    db.add(
        MeasurementRevision(
            **values,
            value=80,
            source_ref=uuid4(),
            ingested_at=NOW + timedelta(hours=4),
            deleted=True,
        )
    )
    db.flush()
    assert evidence_is_stale(db, corrected)


def test_bounded_typed_plan_rejects_sql_and_oversized_window_without_execution(db):
    metric = install(db)
    with pytest.raises(ValidationError):
        spec(metric).model_copy(update={"metric_key": "x; DROP TABLE events"}, deep=True)
        AnalysisSpec.model_validate(
            {**spec(metric).model_dump(), "metric_key": "x; DROP TABLE events"}
        )
    with pytest.raises(ValidationError, match="366 days"):
        AnalysisSpec.model_validate({**spec(metric).model_dump(), "end": NOW + timedelta(days=367)})


def test_formula_ast_enforces_dimensions_and_forbids_code():
    values = {
        "stretch": DimensionedValue(value=30, dimensions={"duration": 1}),
        "sleep": DimensionedValue(value=480, dimensions={"duration": 1}),
    }
    assert evaluate_formula("stretch / sleep", values) == DimensionedValue(value=0.0625)
    with pytest.raises(ValueError, match="incompatible dimensions"):
        evaluate_formula(
            "stretch + score",
            {**values, "score": DimensionedValue(value=4, dimensions={"ordinal": 1})},
        )
    with pytest.raises(ValueError, match="forbidden"):
        evaluate_formula("__import__('os').system('id')", values)


def test_correction_marks_reproducible_snapshot_evidence_stale(db):
    metric = install(db)
    result = execute_analysis(db, spec(metric, method="median"))
    assert not evidence_is_stale(db, result)

    event = db.scalar(select(Event).order_by(Event.start))
    edit = action_for_event(db, event.id)
    form = form_for_action(db, edit.id)
    submit_form(
        db,
        edit.id,
        FormSubmission(
            action_id=edit.id,
            schema_hash=form.schema_hash,
            start=event.start,
            timezone="UTC",
            values={"quality": 2},
            units={"quality": "score_1-5"},
        ),
        actor="test",
    )
    db.flush()
    assert evidence_is_stale(db, result)


def test_generic_plan_is_available_through_shared_typed_tool(db):
    metric = install(db)
    request = spec(metric, method="median")
    result = call_tool(
        db,
        "generic_analysis",
        {"spec": request.model_dump(mode="json")},
    )
    assert result["metric"] == metric.key
    assert result["value"] == 5


def test_as_known_queries_restore_pre_correction_entry_and_observation(db):
    metric = install(db)
    event = db.scalar(select(Event).order_by(Event.start))
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    old_observation = db.scalar(
        select(MetricObservation).where(MetricObservation.source_entry_id == event.id)
    )
    edit = action_for_event(db, event.id)
    form = form_for_action(db, edit.id)
    submit_form(
        db,
        edit.id,
        FormSubmission(
            action_id=edit.id,
            schema_hash=form.schema_hash,
            start=event.start,
            timezone="UTC",
            values={"quality": 2},
            units={"quality": "score_1-5"},
        ),
        actor="test",
    )
    update_audit = db.scalar(
        select(Audit)
        .where(Audit.event_id == event.id, Audit.action == "update")
        .order_by(Audit.id.desc())
    )
    update_audit.created_at = cutoff + timedelta(hours=1)
    old_observation.invalidated_at = cutoff + timedelta(hours=1)
    new_observation = db.scalar(
        select(MetricObservation).where(
            MetricObservation.source_entry_id == event.id,
            MetricObservation.valid.is_(True),
        )
    )
    new_observation.ingested_at = cutoff + timedelta(hours=1)
    new_observation.projection_version = 999
    db.flush()

    entries = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_entries",
            definition_key="user.focus",
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(hours=4),
            knowledge_cutoff=cutoff,
        ),
    )
    observations = execute_analysis(
        db,
        spec(metric, "query_observations", method=None, knowledge_cutoff=cutoff),
    )
    aggregate = execute_analysis(
        db,
        spec(metric, method="median", knowledge_cutoff=cutoff),
    )

    restored = next(row for row in entries["rows"] if row["id"] == str(event.id))
    assert restored["revision"] == 1 and restored["payload"]["quality"] == 1
    restored_observation = next(
        row for row in observations["rows"] if row["source_ref"] == str(event.id)
    )
    assert restored_observation["value"] == 1
    assert restored_observation["source"] == restored["source"]
    assert aggregate["projection_generation"] == old_observation.projection_version
    assert aggregate["input_revisions"][str(event.id)] == 1


def test_as_known_entry_query_uses_definition_from_reconstructed_snapshot(db):
    event = create_event(
        db,
        EventInput(
            start=NOW,
            timezone="UTC",
            payload={"type": "note", "description": "before correction"},
        ),
        actor="test",
    )
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    update_event(
        db,
        event.id,
        EventInput(
            start=NOW,
            timezone="UTC",
            payload={"type": "alcohol", "description": "after correction"},
        ),
        revision=event.revision,
        actor="test",
    )
    update_audit = db.scalar(
        select(Audit)
        .where(Audit.event_id == event.id, Audit.action == "update")
        .order_by(Audit.id.desc())
    )
    update_audit.created_at = cutoff + timedelta(hours=1)
    db.flush()

    def entries(definition_key):
        return execute_analysis(
            db,
            AnalysisSpec(
                operation="query_entries",
                definition_key=definition_key,
                start=NOW - timedelta(minutes=1),
                end=NOW + timedelta(minutes=1),
                knowledge_cutoff=cutoff,
            ),
        )["rows"]

    assert entries("system.note")[0]["payload"]["description"] == "before correction"
    assert entries("system.alcohol") == []


def test_overlap_includes_prior_open_interval_but_not_prior_point(db):
    previous = NOW - timedelta(days=1)
    migraine = create_event(
        db,
        EventInput(start=previous, timezone="UTC", payload={"type": "migraine"}),
        actor="test",
    )
    create_event(
        db,
        EventInput(
            start=previous,
            timezone="UTC",
            payload={"type": "note", "description": "prior point"},
        ),
        actor="test",
    )

    def rows(definition_key):
        return execute_analysis(
            db,
            AnalysisSpec(
                operation="query_entries",
                definition_key=definition_key,
                start=NOW,
                end=NOW + timedelta(days=1),
                knowledge_cutoff=CUTOFF,
                time_relation="overlap",
            ),
        )["rows"]

    assert [row["id"] for row in rows("system.migraine")] == [str(migraine.id)]
    assert rows("system.note") == []


def test_overlap_uses_historical_end_before_correction(db):
    previous = NOW - timedelta(days=1)
    episode = create_event(
        db,
        EventInput(start=previous, timezone="UTC", payload={"type": "migraine"}),
        actor="test",
    )
    before_edit = datetime.now(UTC)
    update_event(
        db,
        episode.id,
        EventInput(
            start=previous,
            end=previous + timedelta(hours=12),
            timezone="UTC",
            payload={"type": "migraine"},
        ),
        revision=episode.revision,
        actor="test",
    )
    update_audit = db.scalar(
        select(Audit).where(Audit.event_id == episode.id).order_by(Audit.created_at.desc())
    )
    update_audit.before = {
        key: value for key, value in update_audit.before.items() if key != "topology"
    }
    db.flush()

    def rows(cutoff):
        return execute_analysis(
            db,
            AnalysisSpec(
                operation="query_entries",
                definition_key="system.migraine",
                start=NOW,
                end=NOW + timedelta(days=1),
                knowledge_cutoff=cutoff,
                time_relation="overlap",
            ),
        )["rows"]

    assert [row["id"] for row in rows(before_edit)] == [str(episode.id)]
    assert rows(before_edit)[0]["topology"] == "open_interval"
    assert rows(datetime.now(UTC) + timedelta(minutes=1)) == []


def test_overlap_infers_legacy_topology_from_historical_kind_after_correction(db):
    previous = NOW - timedelta(days=1)
    episode = create_event(
        db,
        EventInput(start=previous, timezone="UTC", payload={"type": "migraine"}),
        actor="test",
    )
    before_edit = datetime.now(UTC)
    update_event(
        db,
        episode.id,
        EventInput(
            start=previous,
            timezone="UTC",
            payload={"type": "note", "description": "corrected kind"},
        ),
        revision=episode.revision,
        actor="test",
    )
    update_audit = db.scalar(
        select(Audit).where(Audit.event_id == episode.id).order_by(Audit.created_at.desc())
    )
    update_audit.before = {
        key: value for key, value in update_audit.before.items() if key != "topology"
    }
    update_audit.after = {
        key: value for key, value in update_audit.after.items() if key != "topology"
    }
    db.flush()

    rows = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_entries",
            definition_key="system.migraine",
            start=NOW,
            end=NOW + timedelta(days=1),
            knowledge_cutoff=before_edit,
            time_relation="overlap",
        ),
    )["rows"]

    assert [row["id"] for row in rows] == [str(episode.id)]
    assert rows[0]["topology"] == "open_interval"


def test_entry_reconstruction_limit_applies_to_requested_window_not_lifetime(db):
    install(db)
    version_id = db.scalar(select(Event.definition_version_id).where(Event.kind == "user.focus"))
    db.execute(
        insert(Event),
        [
            {
                "id": uuid4(),
                "definition_version_id": version_id,
                "kind": "user.focus",
                "start": NOW - timedelta(days=10, seconds=index),
                "end": None,
                "timezone": "UTC",
                "source": "test",
                "payload": {"quality": 1},
                "topology": "point",
            }
            for index in range(10001)
        ],
    )
    db.flush()

    result = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_entries",
            definition_key="user.focus",
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(hours=4),
            knowledge_cutoff=CUTOFF,
        ),
    )

    assert len(result["rows"]) == 3


def test_entry_analysis_honors_definition_query_permission(db):
    draft = TrackerSetupDraft(
        key="private_focus",
        name="Private focus",
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
    )
    contract = definition_spec(draft).model_copy(
        update={"allowed_operations": {"create", "update", "delete"}}
    )
    definition = create_definition_draft(db, contract, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    create_custom_event(
        db,
        CustomEntryInput(
            definition_key=contract.key,
            start=NOW,
            timezone="UTC",
            values={"quality": 4},
            units={"quality": "score_1-5"},
        ),
        actor="test",
    )
    db.flush()

    result = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_entries",
            definition_key=contract.key,
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(minutes=1),
            knowledge_cutoff=CUTOFF,
        ),
    )

    assert result["rows"] == []


def test_observation_analysis_honors_source_event_query_permission(db):
    metric = install(db)
    event = db.scalar(select(Event).order_by(Event.start))
    draft = TrackerSetupDraft(
        key="private_source",
        name="Private source",
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
    )
    contract = definition_spec(draft).model_copy(
        update={"allowed_operations": {"create", "update", "delete"}}
    )
    definition = create_definition_draft(db, contract, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    version = db.scalar(
        select(EventDefinitionVersion).where(EventDefinitionVersion.definition_id == definition.id)
    )
    event.definition_version_id = version.id
    db.flush()

    result = execute_analysis(db, spec(metric, "query_observations", method=None))

    assert all(row["source_ref"] != str(event.id) for row in result["rows"])


def test_aggregate_lineage_keeps_more_than_one_hundred_event_revisions(db):
    metric = install(db)
    definition = db.scalar(select(Event).limit(1)).definition_version_id
    action_id = f"create:{definition}"
    form = form_for_action(db, action_id)
    for index in range(3, 101):
        submit_form(
            db,
            action_id,
            FormSubmission(
                action_id=action_id,
                schema_hash=form.schema_hash,
                start=NOW + timedelta(minutes=index),
                timezone="UTC",
                values={"quality": 3},
                units={"quality": "score_1-5"},
            ),
            actor="test",
            idempotency_key=f"focus:{index}",
        )
    db.flush()

    result = execute_analysis(db, spec(metric, method="median"))

    assert result["observations"] == 101
    assert len(result["source_refs"]) == 101
    assert len(result["input_revisions"]) == 101


def test_aggregate_staleness_uses_metric_projection_generation(db):
    draft = TrackerSetupDraft(
        key="optional_score",
        name="Optional score",
        locale="en",
        fields=[
            TrackerFieldDraft(
                key="score",
                label="Score",
                kind="scale",
                minimum=1,
                maximum=5,
                required=False,
            )
        ],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    event = create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.optional_score",
            start=NOW,
            timezone="UTC",
            values={"score": 3},
            units={"score": "score_1-5"},
        ),
        actor="test",
    )
    update_custom_event(
        db,
        event.id,
        CustomEntryInput(
            definition_key="user.optional_score",
            start=NOW,
            timezone="UTC",
            values={},
        ),
        revision=event.revision,
        actor="test",
    )
    update_custom_event(
        db,
        event.id,
        CustomEntryInput(
            definition_key="user.optional_score",
            start=NOW,
            timezone="UTC",
            values={"score": 4},
            units={"score": "score_1-5"},
        ),
        revision=event.revision,
        actor="test",
    )
    metric = db.scalar(
        select(MetricDefinition).where(MetricDefinition.key == "user.optional_score.score")
    )
    result = execute_analysis(db, spec(metric, method="median"))

    assert event.revision == 3
    assert set(result["input_revisions"].values()) == {2}
    assert not evidence_is_stale(db, result)

    update_custom_event(
        db,
        event.id,
        CustomEntryInput(
            definition_key="user.optional_score",
            start=NOW,
            timezone="UTC",
            values={"score": 5},
            units={"score": "score_1-5"},
        ),
        revision=event.revision,
        actor="test",
    )
    assert evidence_is_stale(db, result)


def test_tracker_preview_rejects_unregistered_numeric_unit():
    assert (
        TrackerFieldDraft(
            key="weight",
            label="Weight",
            kind="number",
            unit="kg",
            minimum=0,
            maximum=500,
        ).unit
        == "kg"
    )
    with pytest.raises(ValidationError, match="unit is not registered"):
        TrackerFieldDraft(
            key="weight",
            label="Weight",
            kind="number",
            unit="stone",
            minimum=0,
            maximum=500,
        )


def test_model_generic_analysis_honors_scenario_pack_llm_control(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configs["migraine"].llm_enabled = False
    db.flush()
    request = AnalysisSpec(
        operation="query_entries",
        definition_key="system.migraine",
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=1),
        knowledge_cutoff=CUTOFF,
    )

    with pytest.raises(PermissionError, match="migraine"):
        call_tool(
            db,
            "generic_analysis",
            {"spec": request.model_dump(mode="json")},
            for_model=True,
        )
