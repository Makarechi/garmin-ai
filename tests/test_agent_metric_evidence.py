"""Analytical answers reject changed cited metric evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from garmin_ai.agent import AgentStep, ReadCall, answer_question
from garmin_ai.config import Settings
from garmin_ai.i18n import translate
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import AppState, Measurement, MeasurementRevision


@pytest.mark.parametrize("change", ["none", "edit", "insert", "unrelated", "revoke", "external"])
def test_metric_answer_rechecks_only_its_own_source_data(db, db_engine, change):
    now = datetime.now(UTC)
    versions = ensure_system_metric_definitions(db)
    if change == "revoke":
        db.add(
            AppState(key="preferences:onboarding", value={"model_categories": ["health", "diary"]})
        )
        db.flush()

    def add(metric, when, value):
        row = Measurement(
            ts=when,
            metric=metric,
            source="synthetic",
            local_date=when.date(),
            value=value,
            unit=versions[metric].unit,
            metric_definition_version_id=versions[metric].id,
            quality="observed",
            details={},
            ingested_at=now - timedelta(seconds=1),
        )
        db.add(row)
        db.flush()
        return row

    original = add("steps_bucket", now - timedelta(minutes=2), 72)
    if change == "external":
        db.commit()
    spec = {
        "operation": "aggregate_metric",
        "metric_key": "system.steps_bucket",
        "start": (now - timedelta(minutes=10)).isoformat(),
        "end": (now + timedelta(minutes=1)).isoformat(),
        "knowledge_cutoff": now.isoformat(),
        "method": "sum",
    }

    class Provider:
        def __init__(self):
            self.calls = 0

        def structured(self, _instruction, prompt, _schema):
            self.calls += 1
            if self.calls == 1:
                return AgentStep(
                    calls=[
                        ReadCall(name="generic_analysis", arguments_json=json.dumps({"spec": spec}))
                    ]
                )
            evidence = json.loads(prompt)["evidence"][0]
            assert evidence["result"]["value"] == 72
            if change == "edit":
                original.value = 90
                db.flush()
            elif change == "insert":
                inserted = add("steps_bucket", now - timedelta(minutes=1), 90)
                inserted.ingested_at = datetime.now(UTC)
                db.flush()
            elif change == "unrelated":
                add("stress_score", now - timedelta(minutes=1), 50)
            elif change == "revoke":
                db.get(AppState, "preferences:onboarding").value = {"model_categories": ["diary"]}
                db.flush()
            elif change == "external":
                with Session(db_engine) as writer, writer.begin():
                    current = writer.get(Measurement, (original.ts, "steps_bucket", "synthetic"))
                    current.value = 90
            return AgentStep(answer="Synthetic measured result.", evidence_ids=[1])

    provider = Provider()
    result = answer_question(
        db,
        provider,
        "synthetic question",
        Settings(_env_file=None),
        now,
        before_model=db.commit if change == "external" else None,
    )
    assert provider.calls == 2
    if change in {"edit", "insert", "revoke", "external"}:
        assert result == translate("agent.data_changed", "ru")
    else:
        assert "Synthetic measured result." in result


def test_explicit_historical_cutoff_remains_historical_after_a_later_correction(db):
    now = datetime.now(UTC)
    sample_at = now - timedelta(days=2)
    version = ensure_system_metric_definitions(db)["steps_bucket"]
    values = dict(
        ts=sample_at,
        metric="steps_bucket",
        source="synthetic",
        local_date=sample_at.date(),
        value=72,
        unit=version.unit,
        metric_definition_version_id=version.id,
        quality="observed",
        details={},
        source_ref=None,
        ingested_at=now - timedelta(days=2) + timedelta(minutes=1),
    )
    current = Measurement(**values)
    db.add_all([current, MeasurementRevision(**values)])
    db.flush()
    spec = {
        "operation": "aggregate_metric",
        "metric_key": "system.steps_bucket",
        "start": (sample_at - timedelta(minutes=1)).isoformat(),
        "end": (sample_at + timedelta(minutes=1)).isoformat(),
        "knowledge_cutoff": (now - timedelta(days=1)).isoformat(),
        "method": "sum",
    }

    class Provider:
        def __init__(self):
            self.calls = 0

        def structured(self, _instruction, prompt, _schema):
            self.calls += 1
            if self.calls == 1:
                return AgentStep(
                    calls=[
                        ReadCall(name="generic_analysis", arguments_json=json.dumps({"spec": spec}))
                    ]
                )
            assert json.loads(prompt)["evidence"][0]["result"]["value"] == 72
            corrected = {**values, "value": 90, "ingested_at": now - timedelta(hours=1)}
            current.value = 90
            current.ingested_at = corrected["ingested_at"]
            db.add(MeasurementRevision(**corrected))
            db.flush()
            return AgentStep(answer="Synthetic historical result.", evidence_ids=[1])

    result = answer_question(db, Provider(), "synthetic question", Settings(_env_file=None), now)
    assert "Synthetic historical result." in result
