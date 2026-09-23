from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.definitions import (
    CustomEntryInput,
    activate_definition,
    create_custom_event,
    create_definition_draft,
)
from garmin_ai.generic_analytics import (
    AnalysisSpec,
    DimensionedValue,
    evaluate_formula,
    evidence_is_stale,
    execute_analysis,
)
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import (
    AppState,
    Audit,
    Event,
    EventDefinitionVersion,
    Measurement,
    MetricDefinition,
    MetricObservation,
    SourcePayload,
)
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


def test_observation_query_includes_measurement_backed_system_metrics(db):
    payload = SourcePayload(
        source="synthetic",
        endpoint="daily",
        source_key="analytics-sample",
        payload_hash="analytics-synthetic-hash",
        payload={},
        archive_key="synthetic",
        fetched_at=NOW + timedelta(minutes=1),
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
            value=72,
            unit="bpm",
            source_ref=payload.id,
            quality="observed",
            details={},
        )
    )
    ensure_system_metric_definitions(db, backfill=True)

    result = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_observations",
            metric_key="system.heart_rate_bpm",
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(minutes=2),
            knowledge_cutoff=NOW + timedelta(minutes=2),
        ),
    )

    assert len(result["rows"]) == 1
    assert result["rows"][0]["value"] == 72
    assert result["rows"][0]["id"].startswith("measurement:")


def test_boolean_period_comparison_has_no_numeric_difference(db, monkeypatch):
    from garmin_ai import generic_analytics

    results = iter(
        [
            {
                "value": False,
                "metric_version": 1,
                "unit": None,
                "scale_id": None,
                "scale_version": None,
                "method": "latest",
            },
            {
                "value": True,
                "metric_version": 1,
                "unit": None,
                "scale_id": None,
                "scale_version": None,
                "method": "latest",
            },
        ]
    )
    monkeypatch.setattr(generic_analytics, "run_aggregate", lambda *_args, **_kwargs: next(results))
    request = AnalysisSpec(
        operation="compare_periods",
        metric_key="system.synthetic_boolean",
        start=NOW,
        end=NOW + timedelta(hours=1),
        comparison_start=NOW + timedelta(hours=1),
        comparison_end=NOW + timedelta(hours=2),
        method="latest",
        knowledge_cutoff=CUTOFF,
    )

    assert generic_analytics.compare_periods(db, request)["difference"] is None


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
    corrected = execute_analysis(db, spec(metric, method="median"))
    assert corrected["input_revisions"][str(event.id)] == 2
    assert not evidence_is_stale(db, corrected)


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
    assert aggregate["projection_generation"] == old_observation.projection_version
    assert aggregate["input_revisions"][str(event.id)] == 1


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
        select(EventDefinitionVersion).where(
            EventDefinitionVersion.definition_id == definition.id
        )
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


def test_model_generic_analysis_honors_onboarding_data_categories(db):
    db.add(
        AppState(
            key="preferences:onboarding",
            value={"model_categories": ["diary"], "channel": None},
        )
    )
    request = AnalysisSpec(
        operation="aggregate_metric",
        metric_key="system.heart_rate_bpm",
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=1),
        knowledge_cutoff=CUTOFF,
    )

    with pytest.raises(PermissionError, match="health model category"):
        call_tool(
            db,
            "generic_analysis",
            {"spec": request.model_dump(mode="json")},
            for_model=True,
        )
