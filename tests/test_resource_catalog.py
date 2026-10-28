"""Catalog discovery, permission boundaries and raw-source status on synthetic data."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from garmin_ai.agent import AgentStep, ReadCall, answer_question
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.metric_definitions import CoveragePolicy, MetricSpec, _upsert_metric_definition
from garmin_ai.models import (
    EventMetricMapping,
    Measurement,
    MetricDefinition,
    MetricDefinitionVersion,
    SourcePayload,
)
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share
from garmin_ai.tools import call_tool
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

NOW = datetime(2026, 10, 22, 14, tzinfo=UTC)


def metric_spec(key, label):
    return MetricSpec(
        key=key,
        labels={"en": label},
        value_kind="physical_number",
        unit="count",
        dimension="count",
        aggregation="mean",
        allowed_methods={"mean", "max"},
        coverage=CoveragePolicy(kind="sparse"),
        time_semantics="point",
        minimum=0,
        maximum=100,
    )


def test_new_metric_is_discovered_and_described_without_agent_changes(db):
    first = _upsert_metric_definition(
        db, metric_spec("system.a_synthetic", "Synthetic A"), namespace="system"
    )
    _upsert_metric_definition(
        db, metric_spec("system.z_synthetic", "Synthetic Z"), namespace="system"
    )
    db.add(
        Measurement(
            ts=NOW,
            metric="a_synthetic",
            source="synthetic",
            local_date=NOW.date(),
            value=2,
            unit="count",
            metric_definition_version_id=first.id,
            quality="observed",
            details={},
        )
    )
    db.flush()
    first_page = call_tool(db, "resource_catalog", {"kind": "system_metrics", "limit": 1})
    assert first_page["resources"][0]["id"] == "system.a_synthetic"
    assert first_page["next_cursor"] == "system.a_synthetic"
    second_page = call_tool(
        db,
        "resource_catalog",
        {"kind": "system_metrics", "limit": 1, "after_key": first_page["next_cursor"]},
    )
    assert second_page["resources"][0]["id"] == "system.z_synthetic"
    detail = call_tool(
        db,
        "resource_catalog",
        {"kind": "system_metrics", "resource_key": "system.a_synthetic", "detail": True},
    )["resources"][0]
    assert detail["version_id"] == str(first.id)
    assert detail["unit"] == "count"
    assert detail["operations"] == ["max", "mean"]
    assert detail["availability"]["canonical_observations"] == 0
    assert detail["availability"]["measurements"] == 1
    assert detail["availability"]["latest_at"] == NOW.isoformat()
    assert detail["coverage_policy"]["kind"] == "sparse"
    visible = call_tool(db, "resource_catalog", {"kind": "system_metrics"}, for_model=True)
    assert "system.z_synthetic" in {item["id"] for item in visible["resources"]}


def test_agent_can_discover_a_new_metric_without_a_metric_specific_branch(db):
    _upsert_metric_definition(
        db, metric_spec("system.new_synthetic", "Synthetic metric"), namespace="system"
    )

    class Provider:
        def __init__(self):
            self.calls = 0

        def structured(self, _instruction, prompt, _schema):
            payload = json.loads(prompt)
            self.calls += 1
            if self.calls == 1:
                assert "resource_catalog" in {item["name"] for item in payload["tools"]}
                return AgentStep(
                    calls=[
                        ReadCall(
                            name="resource_catalog", arguments_json='{"kind":"system_metrics"}'
                        )
                    ]
                )
            resources = payload["evidence"][0]["result"]["resources"]
            assert "system.new_synthetic" in {item["id"] for item in resources}
            return AgentStep(answer="Synthetic metric is available.", evidence_ids=[1])

    provider = Provider()
    response = answer_question(db, provider, "What can I discover?", Settings(_env_file=None), NOW)
    assert "Synthetic metric is available." in response
    assert "Garmin" in response
    assert "дневник" not in response.casefold()
    assert provider.calls == 2


def test_sensitive_event_and_mapped_metric_require_schema_consent_for_each_destination(db):
    draft = TrackerSetupDraft(
        key="synthetic_secret",
        name="Synthetic private condition",
        privacy="sensitive",
        locale="en",
        fields=[
            TrackerFieldDraft(
                key="value",
                label="Synthetic private field",
                kind="number",
                unit="count",
                minimum=0,
                maximum=100,
            )
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    version_id = UUID(created["action"]["definition_version_id"])
    definition_id = UUID(created["tracker"]["definition_id"])
    mapping = db.scalar(
        select(EventMetricMapping).where(
            EventMetricMapping.event_definition_version_id == version_id
        )
    )
    metric = db.get(MetricDefinitionVersion, mapping.metric_definition_version_id)
    metric_key = db.get(MetricDefinition, metric.definition_id).key
    db.info["channel_destination_instance_id"] = "telegram:primary"
    for kind in ("events", "tracker_metrics"):
        result = call_tool(db, "resource_catalog", {"kind": kind}, for_model=True)
        assert "Synthetic private" not in str(result)
    for destination_kind, instance_id in (
        ("model", "model:gemini:primary"),
        ("channel", "telegram:primary"),
    ):
        grant_tracker_share(
            db,
            TrackerShareConsent(
                definition_id=definition_id,
                destination_kind=destination_kind,
                destination_instance_id=instance_id,
                categories={"schema"},
                granted_at=NOW,
            ),
            authorized=True,
        )
        if destination_kind == "model":
            assert "Synthetic private" not in str(
                call_tool(db, "resource_catalog", {"kind": "tracker_metrics"}, for_model=True)
            )
    result = call_tool(db, "resource_catalog", {"kind": "tracker_metrics"}, for_model=True)
    metric_row = next(item for item in result["resources"] if item["id"] == metric_key)
    assert metric_row["availability"] is None
    assert metric_row["labels"]["en"] == "Synthetic private field"
    event_row = next(
        item
        for item in call_tool(db, "resource_catalog", {"kind": "events"}, for_model=True)[
            "resources"
        ]
        if item["id"] == "user.synthetic_secret"
    )
    assert event_row["availability"] is None
    assert event_row["operations"] == ["query"]


def test_raw_source_is_not_described_as_queryable_or_disclosed(db):
    db.add(
        SourcePayload(
            source="plugin:source:synthetic:private",
            endpoint="source-record-v1",
            source_key="private-identifier",
            payload_hash="synthetic-hash",
            payload={"private": "synthetic payload"},
            archive_key="inline:synthetic",
            fetched_at=NOW,
            parser_version=1,
            status="raw_only",
        )
    )
    db.flush()
    result = call_tool(db, "resource_catalog", {"kind": "raw_sources"}, for_model=True)
    assert result["resources"] == [
        {"id": "source.raw_only", "kind": "source", "availability": "raw_only", "operations": []}
    ]
    assert "private-identifier" not in str(result)
    assert "synthetic payload" not in str(result)


def test_catalog_http_checks_scope_for_requested_resource_kind(db, db_engine):
    key = "synthetic-catalog-http-" + "x" * 32
    client = TestClient(
        create_app(
            Settings(_env_file=None, api_tokens=[ApiToken(key=key, scopes={"read:health"})]),
            db_engine,
        )
    )
    headers = {"Authorization": "Bearer " + key}
    assert (
        client.post(
            "/tools/resource_catalog",
            headers=headers,
            json={"arguments": {"kind": "system_metrics"}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/tools/resource_catalog", headers=headers, json={"arguments": {"kind": "events"}}
        ).status_code
        == 403
    )


def test_catalog_rejects_unbounded_keys_and_conflicting_cursors(db):
    with pytest.raises(ValueError):
        call_tool(db, "resource_catalog", {"kind": "system_metrics", "after_key": "x" * 201})
    with pytest.raises(ValueError):
        call_tool(
            db,
            "resource_catalog",
            {"kind": "system_metrics", "after_key": "system.a", "resource_key": "system.b"},
        )
