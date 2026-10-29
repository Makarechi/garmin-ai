"""Reconfirm source configuration without losing prior record identities."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_source_metric_mapping import install_metric
from test_source_metric_projection import configure, confirm_mapping, source_record, source_registry

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken
from garmin_ai.events import Conflict
from garmin_ai.models import AppState, MetricObservation
from garmin_ai.source_metric_mapping import (
    SourceMetricMappingConfirmation,
    SourceMetricMappingDraft,
    confirm_source_metric_mapping,
    preview_source_metric_mapping,
)
from garmin_ai.source_runtime import poll_source_instance


def test_changed_source_needs_identity_acknowledgement_before_corrections(
    db, db_engine, monkeypatch
):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now, 15)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    configured.integrations[0].config["label"] = "changed"
    contents["source:sample:one"] = source_record(now, 25)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    assert [(r.value, r.valid) for r in db.scalars(select(MetricObservation)).all()] == [(15, True)]
    draft = SourceMetricMappingDraft(
        source_instance_id="source:sample:one",
        metric_version_id=metric.id,
        value_path=["walk_minutes"],
        unit_path=["unit"],
        unit="minutes",
    )
    preview = preview_source_metric_mapping(db, configured, draft)
    assert preview["reconfirmation"]["preserves_record_identity"] is True
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            configured,
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=preview["confirmation_token"]
            ),
        )
    result = confirm_source_metric_mapping(
        db,
        configured,
        SourceMetricMappingConfirmation(
            draft=draft,
            confirmation_token=preview["confirmation_token"],
            reconfirm_source_identity=True,
        ),
    )
    assert result["state"] == "normalized"
    history = db.scalars(
        select(AppState).where(AppState.key.startswith("source-mapping-confirmation:"))
    ).all()
    assert len(history) == 1
    assert history[0].value["previous_source_hash"] != history[0].value["source_hash"]
    db.commit()
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    rows = db.scalars(select(MetricObservation).order_by(MetricObservation.sequence)).all()
    assert [(r.value, r.valid) for r in rows] == [(15, False), (25, True)]


def test_old_preview_cannot_replace_a_newer_confirmation(db):
    metric = install_metric(db)
    original = configure("source:sample:one")
    confirmed = confirm_mapping(db, original, "source:sample:one", metric.id)
    mapping = db.get(AppState, "source-metric-mapping:" + confirmed["id"])
    mapping.value = {**mapping.value, "state": "normalized"}
    db.flush()
    draft = SourceMetricMappingDraft.model_validate(confirmed["mapping"])
    first = configure("source:sample:one")
    second = configure("source:sample:one")
    first.integrations[0].config["label"] = "first"
    second.integrations[0].config["label"] = "second"
    first_preview = preview_source_metric_mapping(db, first, draft)
    second_preview = preview_source_metric_mapping(db, second, draft)
    confirm_source_metric_mapping(
        db,
        first,
        SourceMetricMappingConfirmation(
            draft=draft,
            confirmation_token=first_preview["confirmation_token"],
            reconfirm_source_identity=True,
        ),
    )
    with pytest.raises(Conflict):
        confirm_source_metric_mapping(
            db,
            second,
            SourceMetricMappingConfirmation(
                draft=draft,
                confirmation_token=second_preview["confirmation_token"],
                reconfirm_source_identity=True,
            ),
        )


def test_owner_api_requires_explicit_identity_acknowledgement(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    token = "synthetic-owner-reconfirmation-" + "x" * 32
    configured.api_tokens = [ApiToken(key=token, scopes={"admin"})]
    confirmed = confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    configured.integrations[0].config["label"] = "changed"
    with TestClient(create_app(configured, db_engine)) as client:
        headers = {"Authorization": "Bearer " + token}
        draft = confirmed["mapping"]
        preview = client.post("/source-metric-mappings/preview", json=draft, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["reconfirmation"]["preserves_record_identity"] is True
        body = {"draft": draft, "confirmation_token": preview.json()["confirmation_token"]}
        assert client.post("/source-metric-mappings", json=body, headers=headers).status_code == 409
        assert (
            client.post(
                "/source-metric-mappings",
                json={**body, "reconfirm_source_identity": "yes"},
                headers=headers,
            ).status_code
            == 422
        )
        accepted = client.post(
            "/source-metric-mappings",
            json={**body, "reconfirm_source_identity": True},
            headers=headers,
        )
        assert accepted.status_code == 200 and accepted.json()["state"] == "normalized"
