import asyncio
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from garminconnect import GarminConnectAuthenticationError
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from garmin_ai.accounts import AccountEnrollmentRequired, ensure_account, profile_fingerprint
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
    validate_init_data(valid, "telegram-secret", 42, now=1400, max_age=None)
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

    assert flow.start("signed") == "code_required"
    assert clients[0].password is None
    with pytest.raises(ValueError, match="recently"):
        flow.start("signed")
    assert flow.complete("123456", "signed") == "restored"
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
            raise GarminConnectAuthenticationError("bad code")

    monkeypatch.setattr(garmin_web_auth, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_web_auth, "read_garmin_password", lambda version: "private")
    settings = SimpleNamespace(garmin_email="e", garmin_password_secret_version="v")
    flow = GarminWebAuth(settings, None)
    flow.start("signed")
    for _ in range(3):
        with pytest.raises(ValueError):
            flow.complete("123456", "signed")
    with pytest.raises(ValueError, match="expired"):
        flow.complete("123456", "signed")
    assert flow._client is None


def test_mfa_publication_can_retry_without_reusing_code(monkeypatch):
    from garmin_ai import garmin_web_auth

    calls = []

    class FakeGarmin:
        def __init__(self, **kwargs):
            self.password = kwargs["password"]

        def login(self):
            return "needs_mfa", {"state": "synthetic"}

        def resume_login(self, state, code):
            calls.append((state, code))
            if len(calls) < 3:
                raise GarminConnectAuthenticationError("synthetic wrong code")

    monkeypatch.setattr(garmin_web_auth, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_web_auth, "read_garmin_password", lambda version: "private")
    flow = GarminWebAuth(
        SimpleNamespace(garmin_email="e", garmin_password_secret_version="v"), None
    )
    publications = 0

    def publish(client):
        nonlocal publications
        publications += 1
        if publications == 1:
            raise OSError("synthetic disk failure")

    monkeypatch.setattr(flow, "_publish", publish)
    assert flow.start("signed") == "code_required"
    for _ in range(2):
        with pytest.raises(ValueError, match="Invalid code"):
            flow.complete("123456", "signed")
    with pytest.raises(OSError):
        flow.complete("123456", "signed")
    assert flow.complete("123456", "signed") == "restored"
    assert calls == [({"state": "synthetic"}, "123456")] * 3
    assert publications == 2


def test_abandoned_mfa_client_expires_without_another_request(monkeypatch):
    from garmin_ai import garmin_web_auth

    timers = []

    class FakeTimer:
        def __init__(self, delay, callback, args):
            self.callback = callback
            self.args = args
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

        def fire(self):
            self.callback(*self.args)

    class FakeGarmin:
        def __init__(self, **kwargs):
            self.password = kwargs["password"]

        def login(self):
            return "needs_mfa", None

    monkeypatch.setattr(garmin_web_auth, "Timer", FakeTimer)
    monkeypatch.setattr(garmin_web_auth, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_web_auth, "read_garmin_password", lambda version: "private")
    flow = GarminWebAuth(
        SimpleNamespace(garmin_email="e", garmin_password_secret_version="v"), None
    )
    flow.start("signed")
    assert flow._client is not None
    timers[0].fire()
    assert flow._client is None


def test_completion_requires_the_started_telegram_session(monkeypatch):
    from garmin_ai import garmin_web_auth

    class FakeGarmin:
        def __init__(self, **kwargs):
            self.password = kwargs["password"]

        def login(self):
            return "needs_mfa", None

        def resume_login(self, state, code):
            raise AssertionError("Must reject before checking the code")

    monkeypatch.setattr(garmin_web_auth, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_web_auth, "read_garmin_password", lambda version: "private")
    flow = GarminWebAuth(
        SimpleNamespace(garmin_email="e", garmin_password_secret_version="v"), None
    )
    flow.start("signed")
    with pytest.raises(ValueError, match="does not match"):
        flow.complete("123456", "other-signed")


def test_backup_and_token_publication_have_one_exclusive_gate(db_engine):
    from garmin_ai.db import backup_token_guard

    with backup_token_guard(db_engine), db_engine.connect() as connection:
        assert not connection.scalar(text("SELECT pg_try_advisory_lock(72104626)"))


def test_web_login_restores_only_an_established_owner(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai import garmin_web_auth

    fingerprint = profile_fingerprint({"profileId": 101})
    monkeypatch.setattr(
        garmin_web_auth,
        "GarminReader",
        lambda client: SimpleNamespace(account_fingerprint=lambda: fingerprint),
    )

    class FakeTokens:
        def dump(self, directory):
            from pathlib import Path

            (Path(directory) / "garmin_tokens.json").write_text("synthetic")

    client = SimpleNamespace(client=FakeTokens())
    flow = GarminWebAuth(SimpleNamespace(token_dir=tmp_path / "tokens"), db_engine)
    with pytest.raises(AccountEnrollmentRequired):
        flow._publish(client)
    assert not (tmp_path / "tokens" / "garmin_tokens.json").exists()

    ensure_account(db_engine, fingerprint)
    flow._publish(client)
    assert (tmp_path / "tokens" / "garmin_tokens.json").read_text() == "synthetic"
    db.expire_all()
    assert db.get(AppState, "integration:garmin").value["status"] == "active"


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
        lambda self, init_data: calls.append("start") or "code_required",
    )
    monkeypatch.setattr(
        garmin_web_auth.GarminWebAuth,
        "complete",
        lambda self, code, init_data: calls.append(code) or "restored",
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
    original_validate = garmin_web_auth.validate_init_data

    def validate_after_code_delivery(*args, **kwargs):
        return original_validate(*args, now=int(time.time()) + 301, **kwargs)

    monkeypatch.setattr(garmin_web_auth, "validate_init_data", validate_after_code_delivery)
    assert client.post(
        "/garmin-auth/complete", json={"init_data": signed, "code": "123456"}
    ).json() == {"status": "restored"}
    monkeypatch.setattr(garmin_web_auth, "validate_init_data", original_validate)
    assert calls == ["start", "123456"]
    db.get(AppState, "integration:garmin").value = {"status": "active"}
    db.commit()
    assert client.post("/garmin-auth/start", json={"init_data": signed}).status_code == 409


def test_account_binding_failure_rejects_web_login(db, db_engine, monkeypatch):
    from garmin_ai import garmin_web_auth

    calls = []
    monkeypatch.setattr(
        garmin_web_auth.GarminWebAuth,
        "start",
        lambda self, init_data: calls.append("start") or "code_required",
    )
    db.add(
        AppState(
            key="integration:garmin",
            value={"status": "reauth_required", "reason_class": "AccountMismatch"},
        )
    )
    db.commit()
    settings = Settings(
        telegram_bot_token=SecretStr("telegram-secret"),
        telegram_user_id=42,
        garmin_auth_url="https://example.test/garmin-auth",
    )
    client = TestClient(create_app(settings, db_engine))
    signed = signed_init_data(at=int(time.time()))
    assert client.post("/garmin-auth/start", json={"init_data": signed}).status_code == 409
    assert calls == []


def test_auth_state_check_reports_temporary_database_failure(db, db_engine, monkeypatch):
    from garmin_ai import api

    settings = Settings(
        telegram_bot_token=SecretStr("telegram-secret"),
        telegram_user_id=42,
        garmin_auth_url="https://example.test/garmin-auth",
    )
    client = TestClient(create_app(settings, db_engine))

    @contextmanager
    def unavailable(engine):
        raise SQLAlchemyError("synthetic database failure")
        yield

    monkeypatch.setattr(api, "transaction", unavailable)
    signed = signed_init_data(at=int(time.time()))
    assert client.post("/garmin-auth/start", json={"init_data": signed}).status_code == 503
