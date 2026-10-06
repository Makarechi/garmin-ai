import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.garmin_web_auth import GarminWebAuth, read_garmin_password, validate_init_data
from garmin_ai.models import AppState


def signed_init_data(*, owner=42, at=1000, token="telegram-secret"):
    values = {"auth_date": str(at), "user": json.dumps({"id": owner}, separators=(",", ":"))}
    body = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_mini_app_data_accepts_only_fresh_owner_signature():
    valid = signed_init_data()
    validate_init_data(valid, "telegram-secret", 42, now=1100)
    with pytest.raises(ValueError):
        validate_init_data(valid, "wrong-token", 42, now=1100)
    with pytest.raises(ValueError):
        validate_init_data(valid, "telegram-secret", 43, now=1100)
    with pytest.raises(ValueError):
        validate_init_data(valid, "telegram-secret", 42, now=1400)
    with pytest.raises(ValueError):
        validate_init_data(valid + "&user=duplicated", "telegram-secret", 42, now=1100)


def test_secret_reference_must_pin_a_numeric_version():
    with pytest.raises(RuntimeError):
        read_garmin_password("projects/p/secrets/garmin/versions/latest")


def test_reauthentication_keeps_code_and_password_out_of_persistent_state(monkeypatch):
    from garmin_ai import garmin_web_auth

    clients = []

    class FakeGarmin:
        def __init__(self, email, password, return_on_mfa):
            assert (email, password, return_on_mfa) == ("owner@example.test", "private", True)
            self.password = password
            self.codes = []
            clients.append(self)

        def login(self):
            return "needs_mfa", None

        def resume_login(self, state, code):
            self.codes.append(code)

    monkeypatch.setattr(garmin_web_auth, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_web_auth, "read_garmin_password", lambda version: "private")
    settings = SimpleNamespace(
        garmin_email="owner@example.test",
        garmin_password_secret_version="projects/p/secrets/garmin/versions/1",
    )
    flow = GarminWebAuth(settings, None)
    published = []
    monkeypatch.setattr(flow, "_publish", lambda client: published.append(client))

    assert flow.start() == "code_required"
    assert clients[0].password is None
    with pytest.raises(ValueError, match="recently"):
        flow.start()
    assert flow.complete("123456") == "restored"
    assert clients[0].codes == ["123456"]
    assert published == clients
    assert flow._client is None


def test_reauthentication_rejects_expired_or_excess_code_attempts(monkeypatch):
    from garmin_ai import garmin_web_auth

    class FakeGarmin:
        def __init__(self, **kwargs):
            self.password = kwargs["password"]

        def login(self):
            return "needs_mfa", None

        def resume_login(self, state, code):
            raise ValueError("bad code")

    monkeypatch.setattr(garmin_web_auth, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_web_auth, "read_garmin_password", lambda version: "private")
    settings = SimpleNamespace(garmin_email="e", garmin_password_secret_version="v")
    flow = GarminWebAuth(settings, None)
    flow.start()
    for _ in range(3):
        with pytest.raises(ValueError):
            flow.complete("123456")
    with pytest.raises(ValueError, match="Too many attempts"):
        flow.complete("123456")
    assert flow._client is None


def test_authentication_notice_offers_mini_app_without_exposing_secret(monkeypatch):
    from garmin_ai import runtime

    calls = []

    async def fake_deliver(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(runtime, "deliver", fake_deliver)
    asyncio.run(
        runtime.deliver_connection_notice(
            None,
            None,
            42,
            {"category": "auth", "key": "auth:today"},
            auth_url="https://example.test/garmin-auth",
        )
    )
    args, kwargs = calls[0]
    assert "почтовый код" in args[4]
    assert kwargs["keyboard"]["inline_keyboard"][0][0]["web_app"]["url"] == (
        "https://example.test/garmin-auth"
    )


def test_http_form_requires_signed_owner_and_reauth_state(db, db_engine, monkeypatch):
    from garmin_ai import garmin_web_auth

    calls = []
    monkeypatch.setattr(
        garmin_web_auth.GarminWebAuth,
        "start",
        lambda self: calls.append("start") or "code_required",
    )
    monkeypatch.setattr(
        garmin_web_auth.GarminWebAuth,
        "complete",
        lambda self, code: calls.append(code) or "restored",
    )
    db.add(AppState(key="integration:garmin", value={"status": "reauth_required"}))
    db.commit()
    settings = Settings(
        telegram_bot_token=SecretStr("telegram-secret"),
        telegram_user_id=42,
        garmin_auth_url="https://example.test/garmin-auth",
        garmin_email="owner@example.test",
        garmin_password_secret_version="projects/p/secrets/s/versions/1",
    )
    client = TestClient(create_app(settings, db_engine))
    page = client.get("/garmin-auth")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "private" not in page.text
    assert client.post("/garmin-auth/start", json={"init_data": "forged"}).status_code == 403
    signed = signed_init_data(at=int(time.time()))
    assert client.post("/garmin-auth/start", json={"init_data": signed}).json() == {
        "status": "code_required"
    }
    assert client.post(
        "/garmin-auth/complete", json={"init_data": signed, "code": "123456"}
    ).json() == {"status": "restored"}
    assert calls == ["start", "123456"]
    db.get(AppState, "integration:garmin").value = {"status": "active"}
    db.commit()
    assert client.post("/garmin-auth/start", json={"init_data": signed}).status_code == 409
