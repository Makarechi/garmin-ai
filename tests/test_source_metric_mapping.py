"""Owner-confirmed source mappings use only synthetic configuration."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, IntegrationInstance, Settings
from garmin_ai.events import Conflict
from garmin_ai.models import (
    AppState,
    EventDefinition,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    SourcePayload,
)
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord
from garmin_ai.source_metric_mapping import (
    SourceMetricMappingConfirmation,
    SourceMetricMappingDraft,
    confirm_source_metric_mapping,
    delete_source_metric_mapping,
    list_source_metric_mappings,
    preview_source_metric_mapping,
)
from garmin_ai.source_runtime import poll_source_instance
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


def settings(**kwargs):
    return Settings(
        _env_file=None,
        integrations=[
            IntegrationInstance(id="source:sample:one", kind="source", provider="sample")
        ],
        **kwargs,
    )


def install_metric(db, *, privacy="private"):
    draft = TrackerSetupDraft(
        key="walk",
        name="Walk",
        locale="en",
        privacy=privacy,
        topology="point",
        fields=[
            TrackerFieldDraft(
                key="minutes",
                label="Minutes",
                kind="number",
                unit="minutes",
                minimum=0,
                maximum=240,
            )
        ],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    return db.scalar(
        select(MetricDefinitionVersion)
        .join(MetricDefinition)
        .where(MetricDefinition.key == "user.walk.minutes")
    )


def mapping(version_id, **changes):
    value = {
        "source_instance_id": "source:sample:one",
        "metric_version_id": version_id,
        "value_path": ["walk_minutes"],
        "unit_path": ["unit"],
        "unit": "minutes",
    }
    value.update(changes)
    return SourceMetricMappingDraft.model_validate(value)


def test_confirmation_binds_the_reviewed_mapping_without_importing(db):
    metric = install_metric(db)
    draft = mapping(metric.id)
    preview = preview_source_metric_mapping(db, settings(), draft)
    assert preview["metric"]["id"] == "user.walk.minutes"
    assert preview["mapping"]["value_path"] == ["walk_minutes"]
    assert list_source_metric_mappings(db)["mappings"] == []
    confirmed = confirm_source_metric_mapping(
        db,
        settings(),
        SourceMetricMappingConfirmation(
            draft=draft, confirmation_token=preview["confirmation_token"]
        ),
    )
    assert confirmed["state"] == "confirmed_not_normalized"
    assert list_source_metric_mappings(db)["mappings"] == [confirmed]
    assert db.scalars(select(MetricObservation)).all() == []
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            settings(),
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=preview["confirmation_token"]
            ),
        )


def test_unconfirmed_mapping_keeps_polled_data_raw(db, db_engine, monkeypatch):
    metric = install_metric(db)
    preview_source_metric_mapping(db, settings(), mapping(metric.id))
    db.commit()
    now = datetime(2026, 1, 2, tzinfo=UTC)

    class Source:
        capabilities = SourceCapabilities()

        def read_page(self, *, start, end, cursor, limit):
            return SourcePage(
                instance_id="source:sample:one",
                page_kind="partial",
                fetched_at=now,
                records=[
                    SourceRecord(
                        source_record_id="synthetic-1",
                        observed_at=now - timedelta(hours=1),
                        effective_at=now - timedelta(hours=1),
                        source_timezone="UTC",
                        source_reference="synthetic:1",
                        payload={"walk_minutes": 15, "unit": "minutes"},
                    )
                ],
            )

        def close(self):
            pass

    class Registry:
        def descriptor(self, *_args):
            return SimpleNamespace(plugin_factory=Source)

        def status(self, *_args):
            return SimpleNamespace(available=True)

        def create(self, *_args):
            return Source()

    monkeypatch.setattr("garmin_ai.source_runtime.default_registry", lambda *_args: Registry())
    result = poll_source_instance(db_engine, settings(), "source:sample:one", now=now)
    assert result["status"] == "complete"
    db.expire_all()
    assert db.scalar(select(SourcePayload)).status == "raw_only"
    assert db.scalars(select(MetricObservation)).all() == []


def test_changed_mapping_or_expired_preview_cannot_be_confirmed(db):
    metric = install_metric(db)
    draft = mapping(metric.id)
    preview = preview_source_metric_mapping(db, settings(), draft)
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            settings(),
            SourceMetricMappingConfirmation(
                draft=mapping(metric.id, value_path=["different"]),
                confirmation_token=preview["confirmation_token"],
            ),
        )
    row = db.get(AppState, "source-mapping-preview:" + preview["confirmation_token"])
    row.value = {**row.value, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    db.flush()
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            settings(),
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=preview["confirmation_token"]
            ),
        )
    assert list_source_metric_mappings(db)["mappings"] == []


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "deselected",
        "changed_metric",
        "changed_source",
        "retired_tracker",
    ],
)
def test_confirmation_rechecks_source_and_current_metric(db, change):
    metric = install_metric(db)
    draft = mapping(metric.id)
    configured = settings()
    preview = preview_source_metric_mapping(db, configured, draft)
    if change == "disabled":
        configured.integrations[0].enabled = False
    elif change == "deselected":
        db.add(AppState(key="preferences:onboarding", value={"source_instance_ids": []}))
        db.flush()
    elif change == "changed_metric":
        definition = db.get(MetricDefinition, metric.definition_id)
        definition.current_version += 1
        db.flush()
    elif change == "retired_tracker":
        tracker = db.scalar(select(EventDefinition).where(EventDefinition.key == "user.walk"))
        tracker.status = "retired"
        db.flush()
    else:
        configured.integrations[0].config["label"] = "changed"
    with pytest.raises((Conflict, ValueError)):
        confirm_source_metric_mapping(
            db,
            configured,
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=preview["confirmation_token"]
            ),
        )
    assert list_source_metric_mappings(db)["mappings"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"value_path": []},
        {"value_path": ["x"] * 9},
        {"unit_path": ["walk_minutes"]},
        {"unit_path": ["walk_minutes", "unit"]},
        {"value_path": ["bad\u0000key"]},
    ],
)
def test_invalid_paths_are_rejected(changes):
    with pytest.raises(ValidationError):
        mapping(uuid4(), **changes)


def test_unmatched_unit_and_unknown_source_are_rejected(db):
    metric = install_metric(db)
    with pytest.raises(ValueError):
        preview_source_metric_mapping(db, settings(), mapping(metric.id, unit="hours"))
    with pytest.raises(ValueError):
        preview_source_metric_mapping(
            db, settings(), mapping(metric.id, source_instance_id="source:missing")
        )


def test_reconfirmation_cannot_reuse_mapping_after_source_changes(db):
    metric = install_metric(db)
    draft = mapping(metric.id)
    configured = settings()
    first = preview_source_metric_mapping(db, configured, draft)
    confirmed = confirm_source_metric_mapping(
        db,
        configured,
        SourceMetricMappingConfirmation(
            draft=draft, confirmation_token=first["confirmation_token"]
        ),
    )
    assert "source_hash" not in confirmed
    configured.integrations[0].config["label"] = "changed"
    second = preview_source_metric_mapping(db, configured, draft)
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            configured,
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=second["confirmation_token"]
            ),
        )
    assert delete_source_metric_mapping(db, confirmed["id"]) == {"deleted": True}
    replacement = confirm_source_metric_mapping(
        db,
        configured,
        SourceMetricMappingConfirmation(
            draft=draft, confirmation_token=second["confirmation_token"]
        ),
    )
    assert replacement["state"] == "confirmed_not_normalized"


def test_second_mapping_cannot_silently_replace_confirmed_fields(db):
    metric = install_metric(db)
    first = mapping(metric.id)
    preview = preview_source_metric_mapping(db, settings(), first)
    confirmed = confirm_source_metric_mapping(
        db,
        settings(),
        SourceMetricMappingConfirmation(
            draft=first, confirmation_token=preview["confirmation_token"]
        ),
    )
    second = mapping(metric.id, value_path=["another_value"])
    preview = preview_source_metric_mapping(db, settings(), second)
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            settings(),
            SourceMetricMappingConfirmation(
                draft=second, confirmation_token=preview["confirmation_token"]
            ),
        )
    assert list_source_metric_mappings(db)["mappings"] == [confirmed]


def test_concurrent_tracker_change_requires_retry_without_consuming_preview(db, db_engine):
    metric = install_metric(db)
    draft = mapping(metric.id)
    preview = preview_source_metric_mapping(db, settings(), draft)
    db.commit()
    with Session(db_engine) as other:
        other.scalar(
            select(EventDefinition).where(EventDefinition.key == "user.walk").with_for_update()
        )
        with pytest.raises(Conflict, match="changing"):
            confirm_source_metric_mapping(
                db,
                settings(),
                SourceMetricMappingConfirmation(
                    draft=draft, confirmation_token=preview["confirmation_token"]
                ),
            )
        other.rollback()
    confirmed = confirm_source_metric_mapping(
        db,
        settings(),
        SourceMetricMappingConfirmation(
            draft=draft, confirmation_token=preview["confirmation_token"]
        ),
    )
    assert confirmed["state"] == "confirmed_not_normalized"


def test_mapping_list_hides_other_owner_and_paginates(db):
    metric = install_metric(db)
    draft = mapping(metric.id)
    first = preview_source_metric_mapping(db, settings(), draft)
    confirmed = confirm_source_metric_mapping(
        db,
        settings(),
        SourceMetricMappingConfirmation(
            draft=draft, confirmation_token=first["confirmation_token"]
        ),
    )
    original = db.get(AppState, "source-metric-mapping:" + confirmed["id"])
    second_id = "f" * 64 if confirmed["id"] != "f" * 64 else "e" * 64
    foreign_id = "a" * 64 if confirmed["id"] != "a" * 64 else "b" * 64
    db.add(
        AppState(
            key="source-metric-mapping:" + second_id,
            value={**original.value, "id": second_id},
        )
    )
    db.add(
        AppState(
            key="source-metric-mapping:" + foreign_id,
            value={**original.value, "id": foreign_id, "owner_id": str(uuid4())},
        )
    )
    db.flush()
    page = list_source_metric_mappings(db, limit=1)
    assert len(page["mappings"]) == 1 and page["next_cursor"]
    last = list_source_metric_mappings(db, limit=1, after_id=page["next_cursor"])
    assert len(last["mappings"]) == 1 and last["next_cursor"] is None
    assert {page["mappings"][0]["id"], last["mappings"][0]["id"]} == {confirmed["id"], second_id}
    with pytest.raises(LookupError):
        delete_source_metric_mapping(db, foreign_id)
    original.value = {**original.value, "state": "normalized"}
    db.flush()
    with pytest.raises(Conflict):
        delete_source_metric_mapping(db, confirmed["id"])


def test_api_requires_owner_and_confirms_reviewed_mapping(db, db_engine):
    metric = install_metric(db)
    draft = mapping(metric.id).model_dump(mode="json")
    owner_token = "synthetic-source-map-owner-" + "x" * 32
    limited_token = "synthetic-source-map-limited-" + "x" * 32
    configured = settings(
        api_tokens=[
            ApiToken(key=owner_token, scopes={"admin"}),
            ApiToken(key=limited_token, scopes={"manage:definitions", "manage:integrations"}),
        ]
    )
    db.commit()
    app = create_app(configured, db_engine)
    with TestClient(app) as client:
        schema = app.openapi()
        assert "post" in schema["paths"]["/source-metric-mappings/preview"]
        assert {"get", "post"} <= schema["paths"]["/source-metric-mappings"].keys()
        denied = client.post(
            "/source-metric-mappings/preview",
            json=draft,
            headers={"Authorization": "Bearer " + limited_token},
        )
        assert denied.status_code == 403
        headers = {"Authorization": "Bearer " + owner_token}
        preview = client.post("/source-metric-mappings/preview", json=draft, headers=headers)
        assert preview.status_code == 200, preview.text
        response = client.post(
            "/source-metric-mappings",
            json={"draft": draft, "confirmation_token": preview.json()["confirmation_token"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "confirmed_not_normalized"
        assert client.get("/source-metric-mappings", headers=headers).json()["mappings"] == [
            response.json()
        ]
        removed = client.delete("/source-metric-mappings/" + response.json()["id"], headers=headers)
        assert removed.status_code == 200 and removed.json() == {"deleted": True}
        assert client.get("/source-metric-mappings", headers=headers).json()["mappings"] == []
