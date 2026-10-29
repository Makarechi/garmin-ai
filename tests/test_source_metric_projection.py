"""Synthetic source records become canonical only through a confirmed mapping."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_source_metric_mapping import install_metric

from garmin_ai.analysis_dependencies import metric_dependency, metric_dependency_current
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, IntegrationInstance, Settings
from garmin_ai.metric_definitions import aggregate_metric
from garmin_ai.models import AppState, EventDefinition, MetricObservation, SourcePayload
from garmin_ai.resource_catalog import list_resources
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord
from garmin_ai.source_metric_mapping import (
    SourceMetricMappingConfirmation,
    SourceMetricMappingDraft,
    confirm_source_metric_mapping,
    preview_source_metric_mapping,
)
from garmin_ai.source_runtime import poll_source_instance
from garmin_ai.tools import call_tool


def configure(*instance_ids):
    return Settings(
        _env_file=None,
        integrations=[
            IntegrationInstance(id=identity, kind="source", provider="sample")
            for identity in instance_ids
        ],
    )


def confirm_mapping(db, settings, instance_id, version_id):
    draft = SourceMetricMappingDraft(
        source_instance_id=instance_id,
        metric_version_id=version_id,
        value_path=["walk_minutes"],
        unit_path=["unit"],
        unit="minutes",
    )
    preview = preview_source_metric_mapping(db, settings, draft)
    return confirm_source_metric_mapping(
        db,
        settings,
        SourceMetricMappingConfirmation(
            draft=draft, confirmation_token=preview["confirmation_token"]
        ),
    )


def source_registry(monkeypatch, contents, capabilities=None):
    class Source:
        def __init__(self, instance_id):
            self.instance_id = instance_id
            self.capabilities = capabilities or SourceCapabilities(corrections=True, deletions=True)

        def read_page(self, *, start, end, cursor, limit):
            record = contents[self.instance_id]
            return SourcePage(
                instance_id=self.instance_id,
                page_kind="partial",
                fetched_at=contents["fetched_at"],
                records=[record] if record else [],
            )

        def close(self):
            pass

    class Registry:
        def descriptor(self, *_args):
            return SimpleNamespace(plugin_factory=Source)

        def status(self, *_args):
            return SimpleNamespace(available=True)

        def create(self, instance, _settings):
            return Source(instance.id)

    monkeypatch.setattr("garmin_ai.source_runtime.default_registry", lambda *_args: Registry())


def source_record(now, value=15, *, unit="minutes", operation="upsert"):
    return SourceRecord(
        source_record_id="synthetic-record",
        operation=operation,
        observed_at=now - timedelta(hours=1),
        effective_at=now - timedelta(hours=1),
        source_timezone="UTC",
        source_reference="synthetic:record",
        payload={"walk_minutes": value, "unit": unit} if operation == "upsert" else {},
    )


def test_confirmed_source_is_queryable_and_replay_is_idempotent(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    for _ in range(2):
        assert (
            poll_source_instance(db_engine, configured, "source:sample:one", now=now)["status"]
            == "complete"
        )
    db.expire_all()
    rows = db.scalars(select(MetricObservation)).all()
    assert len(rows) == 1 and rows[0].valid and rows[0].value == 15
    result = aggregate_metric(
        db, "user.walk.minutes", now - timedelta(days=1), now + timedelta(seconds=1)
    )
    assert result["value"] == 15 and result["observations"] == 1
    catalog = list_resources(
        db,
        kind="tracker_metrics",
        after_key=None,
        resource_key="user.walk.minutes",
        limit=10,
        detail=True,
    )
    assert catalog["resources"][0]["availability"]["canonical_observations"] == 1


def test_mapping_can_project_an_already_polled_record_when_seen_again(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert db.scalars(select(MetricObservation)).all() == []
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 1
    assert db.scalar(select(MetricObservation)).value == 15


def test_revision_reversion_bad_unit_and_delete_never_leave_stale_value(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    for record in [source_record(now, 15), source_record(now, 25), source_record(now, 15)]:
        contents["source:sample:one"] = record
        poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    rows = db.scalars(
        select(MetricObservation).order_by(
            MetricObservation.ingested_at, MetricObservation.sequence
        )
    ).all()
    assert [row.value for row in rows] == [15, 25, 15]
    assert [row.valid for row in rows] == [False, False, True]
    contents["source:sample:one"] = source_record(now, 15, unit="hours")
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert not any(row.valid for row in db.scalars(select(MetricObservation)).all())
    contents["source:sample:one"] = source_record(now, operation="delete")
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert not any(row.valid for row in db.scalars(select(MetricObservation)).all())
    assert len(db.scalars(select(SourcePayload)).all()) == 4


def test_identical_source_record_ids_are_isolated_between_instances(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one", "source:sample:two")
    for instance in ("source:sample:one", "source:sample:two"):
        confirm_mapping(db, configured, instance, metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {
        "fetched_at": now,
        "source:sample:one": source_record(now, 15),
        "source:sample:two": source_record(now, 25),
    }
    source_registry(monkeypatch, contents)
    for instance in ("source:sample:one", "source:sample:two"):
        poll_source_instance(db_engine, configured, instance, now=now)
    db.expire_all()
    rows = db.scalars(select(MetricObservation).where(MetricObservation.valid.is_(True))).all()
    assert {row.value for row in rows} == {15, 25}
    assert len({row.account for row in rows}) == 2


@pytest.mark.parametrize("bad_value", [True, "15", -1, 241, 2**53 + 1, None])
def test_invalid_source_values_do_not_create_a_metric(db, db_engine, monkeypatch, bad_value):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now, bad_value)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert db.scalars(select(MetricObservation)).all() == []
    assert db.scalar(select(SourcePayload)).status == "raw_only"


def test_change_in_time_semantics_quarantines_and_recovers_same_revision(
    db, db_engine, monkeypatch
):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    capabilities = SourceCapabilities(corrections=True, deletions=True)
    source_registry(monkeypatch, contents, capabilities)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    capabilities.time_semantics = "calendar_day"
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert not any(row.valid for row in db.scalars(select(MetricObservation)).all())
    capabilities.time_semantics = "instant"
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    rows = db.scalars(select(MetricObservation).order_by(MetricObservation.sequence)).all()
    assert [row.valid for row in rows] == [False, True]
    assert [row.sequence for row in rows] == [1, 3]


def test_new_source_configuration_cannot_reuse_prior_confirmation(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    configured.integrations[0].config["label"] = "changed"
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert db.scalars(select(MetricObservation)).all() == []


def test_sensitive_source_metric_respects_model_consent(db, db_engine, monkeypatch):
    metric = install_metric(db, privacy="sensitive")
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    arguments = {
        "spec": {
            "operation": "aggregate_metric",
            "metric_key": "user.walk.minutes",
            "start": (now - timedelta(days=1)).isoformat(),
            "end": (now + timedelta(seconds=1)).isoformat(),
            "knowledge_cutoff": datetime.now(UTC).isoformat(),
        }
    }
    with pytest.raises(PermissionError):
        call_tool(db, "generic_analysis", arguments, for_model=True)
    tracker = db.scalar(select(EventDefinition).where(EventDefinition.key == "user.walk"))
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=tracker.id,
            destination_kind="model",
            destination_instance_id="model:gemini:primary",
            categories={"schema", "facts"},
            granted_at=datetime.now(UTC),
        ),
        authorized=True,
    )
    result = call_tool(db, "generic_analysis", arguments, for_model=True)
    assert result["value"] == 15


def test_updated_source_invalidates_existing_analysis_dependency(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    requested = datetime.now(UTC)
    arguments = {
        "spec": {
            "operation": "aggregate_metric",
            "metric_key": "user.walk.minutes",
            "start": (now - timedelta(days=1)).isoformat(),
            "end": (now + timedelta(seconds=1)).isoformat(),
            "knowledge_cutoff": requested.isoformat(),
        }
    }
    result = call_tool(db, "generic_analysis", arguments, for_model=True)
    dependency = metric_dependency(
        {"tool": "generic_analysis", "arguments": arguments, "result": result}, requested
    )
    assert dependency and metric_dependency_current(db, dependency)
    db.commit()
    contents["source:sample:one"] = source_record(now, 25)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert not metric_dependency_current(db, dependency)


def test_projected_metric_requires_health_scope(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    diary_token = "synthetic-diary-only-" + "x" * 32
    health_token = "synthetic-health-diary-" + "x" * 32
    configured.api_tokens = [
        ApiToken(key=diary_token, scopes={"read:diary"}),
        ApiToken(key=health_token, scopes={"read:diary", "read:health"}),
    ]
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    spec = {
        "operation": "aggregate_metric",
        "metric_key": "user.walk.minutes",
        "start": (now - timedelta(days=1)).isoformat(),
        "end": (now + timedelta(seconds=1)).isoformat(),
        "knowledge_cutoff": datetime.now(UTC).isoformat(),
    }
    with TestClient(create_app(configured, db_engine)) as client:
        diary = {"Authorization": "Bearer " + diary_token}
        health = {"Authorization": "Bearer " + health_token}
        denied = client.post(
            "/tools/generic_analysis", json={"arguments": {"spec": spec}}, headers=diary
        )
        assert denied.status_code == 403
        allowed = client.post(
            "/tools/generic_analysis", json={"arguments": {"spec": spec}}, headers=health
        )
        assert allowed.status_code == 200 and allowed.json()["value"] == 15
        catalog = client.post(
            "/tools/resource_catalog",
            json={"arguments": {"kind": "tracker_metrics", "resource_key": "user.walk.minutes"}},
            headers=diary,
        )
        assert catalog.status_code == 200 and catalog.json()["resources"][0]["availability"] is None
        # A deleted source fact remains accessible through historical queries.
        contents["source:sample:one"] = source_record(now, operation="delete")
        poll_source_instance(db_engine, configured, "source:sample:one", now=now)
        assert (
            client.post(
                "/tools/generic_analysis", json={"arguments": {"spec": spec}}, headers=diary
            ).status_code
            == 403
        )


def test_projected_metric_requires_health_model_category(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    db.add(AppState(key="preferences:onboarding", value={"model_categories": ["diary"]}))
    db.flush()
    spec = {
        "operation": "aggregate_metric",
        "metric_key": "user.walk.minutes",
        "start": (now - timedelta(days=1)).isoformat(),
        "end": (now + timedelta(seconds=1)).isoformat(),
        "knowledge_cutoff": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(PermissionError):
        call_tool(db, "generic_analysis", {"spec": spec}, for_model=True)
    catalog = call_tool(
        db,
        "resource_catalog",
        {"kind": "tracker_metrics", "resource_key": "user.walk.minutes"},
        for_model=True,
    )
    assert catalog["resources"][0]["availability"] is None
