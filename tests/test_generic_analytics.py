from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.generic_analytics import (
    AnalysisSpec,
    DimensionedValue,
    evaluate_formula,
    evidence_is_stale,
    execute_analysis,
)
from garmin_ai.models import Event, MetricDefinition
from garmin_ai.tools import call_tool
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    action_for_event,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)

NOW = datetime(2026, 9, 20, 18, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 21, 18, tzinfo=UTC)


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
