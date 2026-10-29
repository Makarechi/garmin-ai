"""Threshold analysis uses bounded, synthetic measurement histories."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import insert

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.generic_analytics import AnalysisSpec, evidence_is_stale, execute_analysis, spec_hash
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import Measurement, MeasurementRevision
from garmin_ai.tools import call_tool

START = datetime(2026, 1, 15, 12, tzinfo=UTC)


def plan(**overrides):
    values = {
        "operation": "threshold_segments",
        "metric_key": "system.stress_score",
        "start": START,
        "end": START + timedelta(minutes=15),
        "threshold": 51,
        "threshold_operator": "gte",
        "knowledge_cutoff": START + timedelta(days=1),
    }
    return AnalysisSpec(**(values | overrides))


def measurements(db, points, *, source="synthetic"):
    version = ensure_system_metric_definitions(db)["stress_score"]
    for minute, value in points:
        at = START + timedelta(minutes=minute)
        db.add(
            Measurement(
                ts=at,
                metric="stress_score",
                source=source,
                local_date=at.date(),
                value=value,
                unit="score",
                metric_definition_version_id=version.id,
                quality="observed",
                details={},
                ingested_at=START + timedelta(hours=1),
            )
        )
    db.flush()


def test_threshold_intervals_separate_gaps_and_count_observed_time(db):
    measurements(db, [(0, 10), (1, 65), (2, 80), (3, 20), (13, 75), (14, 70), (15, 10)])
    result = call_tool(db, "generic_analysis", {"spec": plan().model_dump(mode="json")})
    assert [(s["start"], s["end"], s["peak"]) for s in result["segments"]] == [
        (
            (START + timedelta(minutes=1)).isoformat(),
            (START + timedelta(minutes=3)).isoformat(),
            80,
        ),
        (
            (START + timedelta(minutes=13)).isoformat(),
            (START + timedelta(minutes=15)).isoformat(),
            75,
        ),
    ]
    assert result["covered_seconds"] == 300
    assert result["matching_seconds"] == 240
    assert result["coverage_ratio"] == pytest.approx(1 / 3)
    assert result["source"] == "measurement:synthetic"
    assert not evidence_is_stale(db, result)
    record = db.get(Measurement, (START + timedelta(minutes=2), "stress_score", "synthetic"))
    record.value = 85
    db.flush()
    assert evidence_is_stale(db, result)


def test_one_sample_does_not_claim_a_duration_or_zero_match(db):
    measurements(db, [(1, 80)])
    result = execute_analysis(db, plan())
    assert result["segments"] == []
    assert result["covered_seconds"] == 0
    assert result["matching_seconds"] is None


def test_future_timestamp_is_not_known_at_the_selected_cutoff(db):
    measurements(db, [(120, 80), (121, 80)])
    result = execute_analysis(
        db,
        plan(end=START + timedelta(hours=3), knowledge_cutoff=START + timedelta(minutes=90)),
    )
    assert result["matching_seconds"] is None
    assert result["covered_seconds"] == 0


def test_sample_limit_is_enforced_without_partial_interpretation(db):
    measurements(db, [(0, 80), (1, 80), (2, 80)])
    with pytest.raises(ValueError, match="explicit result limit"):
        execute_analysis(db, plan(limit=2))


def test_threshold_operation_obeys_http_scope_and_returns_intervals(db, db_engine):
    measurements(db, [(0, 80), (1, 80), (2, 10)])
    db.commit()
    health_key = "synthetic-health-credential-" + "h" * 24
    diary_key = "synthetic-diary-credential-" + "d" * 24
    client = TestClient(
        create_app(
            Settings(
                _env_file=None,
                api_tokens=[
                    ApiToken(key=health_key, scopes={"read:health"}),
                    ApiToken(key=diary_key, scopes={"read:diary"}),
                ],
            ),
            db_engine,
        )
    )
    payload = {"arguments": {"spec": plan().model_dump(mode="json")}}
    response = client.post(
        "/tools/generic_analysis", json=payload, headers={"Authorization": "Bearer " + health_key}
    )
    assert response.status_code == 200
    assert response.json()["matching_seconds"] == 120
    denied = client.post(
        "/tools/generic_analysis", json=payload, headers={"Authorization": "Bearer " + diary_key}
    )
    assert denied.status_code == 403


def test_sources_must_be_selected_before_measuring_intervals(db):
    measurements(db, [(0, 80), (1, 80)])
    measurements(db, [(0, 20), (1, 20)], source="second")
    with pytest.raises(ValueError, match="Multiple metric sources"):
        execute_analysis(db, plan())
    result = execute_analysis(db, plan(source="measurement:synthetic"))
    assert result["matching_seconds"] == 60
    assert result["source"] == "measurement:synthetic"


def test_boundary_intervals_are_clipped_to_the_requested_window(db):
    measurements(db, [(-1, 80), (1, 80), (2, 10)])
    result = execute_analysis(db, plan(end=START + timedelta(minutes=2)))
    assert result["covered_seconds"] == 120
    assert result["matching_seconds"] == 120
    assert result["segments"][0]["start"] == START.isoformat()
    assert result["segments"][0]["end"] == (START + timedelta(minutes=2)).isoformat()


@pytest.mark.parametrize("changed_contract", [False, True])
def test_later_invalid_revision_does_not_restore_or_bridge_old_values(db, changed_contract):
    measurements(db, [(0, 80), (1, 80), (2, 80), (3, 80)])
    current = db.get(Measurement, (START + timedelta(minutes=1), "stress_score", "synthetic"))
    version_id = current.metric_definition_version_id
    if changed_contract:
        version_id = ensure_system_metric_definitions(db)["body_battery"].id
    values = dict(
        ts=current.ts,
        metric=current.metric,
        source=current.source,
        local_date=current.local_date,
        value=current.value,
        unit=current.unit,
        details={},
        source_ref=None,
    )
    db.add_all(
        [
            MeasurementRevision(
                **values,
                metric_definition_version_id=current.metric_definition_version_id,
                quality="observed",
                ingested_at=START + timedelta(hours=1),
            ),
            MeasurementRevision(
                **values,
                metric_definition_version_id=version_id,
                quality="observed" if changed_contract else "invalid",
                ingested_at=START + timedelta(hours=2),
            ),
        ]
    )
    db.flush()
    old = execute_analysis(
        db,
        plan(
            end=START + timedelta(minutes=3),
            knowledge_cutoff=START + timedelta(hours=1, minutes=30),
        ),
    )
    new = execute_analysis(
        db, plan(end=START + timedelta(minutes=3), knowledge_cutoff=START + timedelta(hours=3))
    )
    assert old["matching_seconds"] == 180
    assert new["matching_seconds"] == 60
    assert not evidence_is_stale(db, new)
    assert new["segments"][0]["start"] == (START + timedelta(minutes=2)).isoformat()
    rows = execute_analysis(
        db,
        AnalysisSpec(
            operation="query_observations",
            metric_key="system.stress_score",
            start=START,
            end=START + timedelta(minutes=3),
            knowledge_cutoff=START + timedelta(hours=3),
        ),
    )["rows"]
    assert len(rows) == 2
    aggregate = execute_analysis(
        db,
        AnalysisSpec(
            operation="aggregate_metric",
            metric_key="system.stress_score",
            start=START,
            end=START + timedelta(minutes=3),
            knowledge_cutoff=START + timedelta(hours=3),
        ),
    )
    assert aggregate["observations"] == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"threshold": float("nan")},
        {"threshold": True},
        {"threshold": "51"},
        {"threshold_operator": None},
        {"end": START + timedelta(days=32)},
        {"method": "sum"},
    ],
)
def test_threshold_plan_rejects_unbounded_or_invalid_parameters(updates):
    with pytest.raises(ValidationError):
        plan(**updates)


def test_non_time_weighted_metrics_do_not_fabricate_intervals(db):
    ensure_system_metric_definitions(db)
    with pytest.raises(ValueError, match="time-weighted"):
        execute_analysis(db, plan(metric_key="system.steps_bucket"))


def test_aggregate_rejects_saturated_snapshot_before_filtering_tombstones(db):
    version = ensure_system_metric_definitions(db)["stress_score"]
    common = {
        "metric": "stress_score",
        "source": "synthetic",
        "value": 70,
        "unit": "score",
        "metric_definition_version_id": version.id,
        "quality": "observed",
        "details": {},
        "ingested_at": START + timedelta(hours=4),
        "source_ref": None,
    }
    rows = []
    for index in range(10002):
        at = START + timedelta(seconds=index)
        rows.append({**common, "ts": at, "local_date": at.date(), "deleted": index < 10001})
    db.execute(insert(MeasurementRevision), rows)
    db.flush()
    spec = AnalysisSpec(
        operation="aggregate_metric",
        metric_key="system.stress_score",
        method="max",
        start=START,
        end=START + timedelta(hours=3),
        knowledge_cutoff=START + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="exceeds 10000 observations"):
        execute_analysis(db, spec)


def test_existing_analysis_spec_keeps_its_pre_threshold_hash():
    existing = AnalysisSpec(
        operation="query_observations",
        metric_key="system.stress_score",
        start=START,
        end=START + timedelta(minutes=15),
        knowledge_cutoff=START + timedelta(days=1),
    )
    original = existing.model_dump(
        mode="json", exclude={"threshold", "threshold_operator", "timezone"}
    )
    expected = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert spec_hash(existing) == expected
    assert spec_hash(plan(threshold=50)) != spec_hash(plan(threshold=51))
