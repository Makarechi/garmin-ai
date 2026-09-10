from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.events import Conflict
from garmin_ai.hypotheses import HypothesisSpec, recheck, register, stop
from garmin_ai.models import HealthDay, TimelineInterval

NOW = datetime(2026, 9, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def configured_data_timezone(db):
    db.info["timezone"] = "UTC"


def spec(**changes):
    values = dict(
        id=uuid4(),
        question="Does late caffeine accompany lower sleep scores?",
        discovery_start="2026-09-01",
        discovery_end="2026-09-08",
        validation_start="2026-09-11",
        validation_end="2026-09-20",
        expires="2026-09-30",
        timezone="UTC",
        direction="lower",
    )
    return HypothesisSpec(**{**values, **changes})


def test_prospective_registration_is_immutable_and_idempotent(db):
    protocol = spec()
    value = register(db, protocol, NOW)
    assert register(db, protocol, NOW) == value
    with pytest.raises(Conflict):
        register(db, protocol.model_copy(update={"direction": "higher"}), NOW)
    with pytest.raises(ValueError, match="prospective"):
        register(db, spec(validation_start="2026-09-10"), NOW)
    with pytest.raises(ValidationError):
        spec(validation_start="2026-09-08")
    with pytest.raises(ValidationError, match="Unknown timezone"):
        spec(timezone="invalid/synthetic")


def test_recheck_keeps_previous_negative_and_changed_evidence(db):
    protocol = spec()
    register(db, protocol, NOW)
    checked_at = NOW + timedelta(days=11)
    first = recheck(db, protocol.id, checked_at)
    assert first["check_count"] == 1
    assert first["checks"][0]["conclusion"] == "insufficient_evidence"
    assert recheck(db, protocol.id, checked_at)["check_count"] == 1
    end = NOW + timedelta(days=2, hours=7)
    db.add(HealthDay(day=end.date(), sleep_score=80, sources={"field:sleep_score": "synthetic"}))
    db.add(
        TimelineInterval(
            id=f"sleep:{end.date()}",
            start=end - timedelta(hours=8),
            end=end,
            label="sleep",
            source="garmin",
            confidence=1,
            evidence={"source_ref": "synthetic"},
        )
    )
    db.flush()
    revised = recheck(db, protocol.id, checked_at)
    assert revised["check_count"] == 2
    assert revised["checks"][0] == first["checks"][0]
    assert (
        revised["checks"][0]["evidence"]["evidence_hash"]
        != revised["checks"][1]["evidence"]["evidence_hash"]
    )


def test_stop_expiry_and_incomplete_period_prevent_checks(db):
    protocol = spec()
    register(db, protocol, NOW)
    with pytest.raises(ValueError, match="not finished"):
        recheck(db, protocol.id, NOW)
    with pytest.raises(Conflict, match="expired"):
        recheck(db, protocol.id, NOW + timedelta(days=21))
    assert stop(db, protocol.id, NOW)["status"] == "stopped"
    assert stop(db, protocol.id, NOW)["status"] == "stopped"
    with pytest.raises(Conflict, match="stopped"):
        recheck(db, protocol.id, NOW + timedelta(days=11))


def test_http_protocols_require_scopes_and_can_stop(db, db_engine):
    protocol = spec()
    register(db, protocol, NOW)
    db.commit()
    settings = Settings(api_key="synthetic-admin-key-" + "x" * 32)
    client = TestClient(create_app(settings, db_engine))
    assert client.get(f"/hypotheses/{protocol.id}").status_code == 401
    headers = {"Authorization": "Bearer " + settings.api_key.get_secret_value()}
    response = client.get(f"/hypotheses/{protocol.id}", headers=headers)
    assert response.status_code == 200 and response.json()["check_count"] == 0
    assert (
        client.post(f"/hypotheses/{protocol.id}/stop", headers=headers).json()["status"]
        == "stopped"
    )


@pytest.mark.parametrize(
    "interval, conclusion",
    [
        ([-4, -1], "direction_repeated_observationally"),
        ([1, 4], "opposite_direction"),
        ([-1, 1], "not_distinguished"),
        (None, "not_distinguished"),
    ],
)
def test_fixed_direction_keeps_negative_and_uncertain_results(
    db, monkeypatch, interval, conclusion
):
    from garmin_ai import hypotheses

    protocol = spec()
    register(db, protocol, NOW)
    monkeypatch.setattr(
        hypotheses,
        "guarded_analysis",
        lambda *args: {
            "status": "exploratory",
            "comparison": {"ci95": interval},
            "evidence_hash": "synthetic",
        },
    )
    result = recheck(db, protocol.id, NOW + timedelta(days=11))
    assert result["checks"][0]["conclusion"] == conclusion
    assert "not a treatment experiment" in result["interpretation"]


@pytest.mark.parametrize(
    "scopes",
    [{"read:diary"}, {"read:health", "read:diary"}, {"read:health", "read:diary", "write:diary"}],
)
def test_api_read_and_stop_enforce_combined_scopes(db, db_engine, scopes):
    from garmin_ai.config import ApiToken

    protocol = spec()
    register(db, protocol, NOW)
    db.commit()
    key = "synthetic-scoped-hypothesis-key-123456"
    client = TestClient(
        create_app(Settings(api_tokens=[ApiToken(key=key, scopes=scopes)]), db_engine)
    )
    headers = {"Authorization": "Bearer " + key}
    assert client.get(f"/hypotheses/{protocol.id}", headers=headers).status_code == (
        200 if {"read:health", "read:diary"} <= scopes else 403
    )
    assert client.post(f"/hypotheses/{protocol.id}/stop", headers=headers).status_code == (
        200 if {"read:health", "read:diary", "write:diary"} <= scopes else 403
    )


def test_protocol_cannot_backdate_validation_with_client_timezone(db):
    db.info["timezone"] = "Pacific/Kiritimati"
    with pytest.raises(ValueError, match="configured data timezone"):
        register(db, spec(timezone="Pacific/Honolulu", validation_start="2026-09-10"), NOW)


def test_recheck_refuses_changed_data_timezone(db):
    protocol = spec()
    register(db, protocol, NOW)
    db.info["timezone"] = "Asia/Tokyo"
    with pytest.raises(ValueError, match="configured data timezone"):
        recheck(db, protocol.id, NOW + timedelta(days=11))


def test_repeated_older_evidence_does_not_consume_another_check(db, monkeypatch):
    from garmin_ai import hypotheses

    protocol = spec()
    register(db, protocol, NOW)
    for evidence_hash in ["A", "B", "A"]:
        monkeypatch.setattr(
            hypotheses,
            "guarded_analysis",
            lambda *args, evidence_hash=evidence_hash: {
                "status": "insufficient_evidence",
                "comparison": None,
                "evidence_hash": evidence_hash,
            },
        )
        value = recheck(db, protocol.id, NOW + timedelta(days=11))
    assert value["check_count"] == 2
    assert [check["evidence"]["evidence_hash"] for check in value["checks"]] == ["A", "B"]


def test_minimum_expiry_allows_one_complete_recheck_day(db):
    with pytest.raises(ValidationError, match="2 to 31"):
        spec(expires="2026-09-21")
    protocol = spec(expires="2026-09-22")
    register(db, protocol, NOW)
    assert recheck(db, protocol.id, NOW + timedelta(days=11))["check_count"] == 1
