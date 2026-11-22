"""Compare the local Mini App and signed owner checks with pinned Python."""

import argparse
import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import Settings

AUTH_URL = "https://synthetic.invalid/garmin-auth"
BOT = "9000:synthetic-garmin-test"


def signed(auth_at, owner=42, *, bot=BOT, padding=None):
    fields = {
        "auth_date": str(auth_at),
        "user": json.dumps({"id": owner, "first_name": "Fictional + & ☕"}, ensure_ascii=False),
        "query_id": "synthetic + &",
        "signature": "fictional-telegram-field",
    }
    if padding is not None:
        fields["signature"] = "Ж" * padding
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items())).encode()
    secret = hmac.digest(b"WebAppData", bot.encode(), hashlib.sha256)
    fields["hash"] = hmac.new(secret, check, hashlib.sha256).hexdigest()
    if padding is not None:
        signature = fields.pop("signature")
        return urlencode(fields) + f"&signature={signature}"
    return urlencode(fields)


def status(engine, value, reason=None):
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO app_state(key,value) VALUES ('integration:garmin',CAST(:value AS jsonb))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """),
            {"value": json.dumps({"status": value, "reason_class": reason, "failure_count": 0})},
        )


def verify(engine, binary, directory):
    settings = Settings(
        _env_file=None,
        telegram_bot_token=BOT,
        telegram_user_id=42,
        garmin_auth_url=AUTH_URL,
        data_dir=directory / "data",
        token_dir=directory / "tokens",
        lock_dir=directory / "state",
        database_url=engine.url.render_as_string(hide_password=False),
    )
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO channel_bindings
                  (id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
                SELECT gen_random_uuid(),id,'telegram','primary','42','fixture' FROM people
                WHERE singleton ON CONFLICT DO NOTHING
            """)
        )
        connection.execute(
            text("""
                INSERT INTO app_state(key,value) VALUES
                  ('account:garmin',jsonb_build_object('fingerprint',repeat('a',64)))
            """)
        )
    status(engine, "active")
    oracle = TestClient(create_app(settings, engine=engine))
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_GARMIN_AUTH_URL": AUTH_URL,
        "GA_TELEGRAM_BOT_TOKEN": BOT,
        "GA_TELEGRAM_USER_ID": "42",
        "GA_DATA_DIR": str(settings.data_dir),
        "GA_TOKEN_DIR": str(settings.token_dir),
        "GA_LOCK_DIR": str(settings.lock_dir),
    }
    with subprocess.Popen(
        [binary, "serve", "--port", "0"],
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            prefix = "Listening on "
            line = server.stdout.readline().strip()
            assert line.startswith(prefix)
            with httpx.Client(base_url=line.removeprefix(prefix), timeout=3) as native:
                reference = oracle.get("/garmin-auth")
                actual = native.get("/garmin-auth")
                assert actual.status_code == reference.status_code == 200
                assert actual.content == reference.content
                for key in (
                    "content-type",
                    "content-security-policy",
                    "cache-control",
                    "referrer-policy",
                ):
                    assert actual.headers[key] == reference.headers[key]

                def compare(route, data, expected):
                    old = oracle.post(f"/garmin-auth/{route}", json={"init_data": data})
                    new = native.post(f"/garmin-auth/{route}", json={"init_data": data})
                    assert old.status_code == new.status_code == expected, (
                        route,
                        expected,
                        old.status_code,
                        new.status_code,
                    )
                    assert old.json() == new.json()
                    assert new.headers["cache-control"] == "no-store"

                now = int(time.time())
                current = signed(now)
                stale = signed(now - 400)
                compare("start", current, 200)
                compare("start", signed(now, 43), 403)
                compare("start", signed(now, bot="synthetic-wrong-bot"), 403)
                compare("start", signed(now + 90), 403)
                compare("start", stale, 403)
                compare("start", current + "&%75ser=%7B%7D", 403)
                compare("start", current + "&malformed", 403)

                document = json.dumps({"init_data": current})
                for encoding in ("utf-8-sig", "utf-16", "utf-32"):
                    arguments = {
                        "content": document.encode(encoding),
                        "headers": {"Content-Type": "application/json"},
                    }
                    old = oracle.post("/garmin-auth/start", **arguments)
                    new = native.post("/garmin-auth/start", **arguments)
                    assert old.status_code == new.status_code == 200, (
                        encoding,
                        old.status_code,
                        new.status_code,
                    )
                    assert old.json() == new.json() == {"status": "already_connected"}
                    assert new.headers["cache-control"] == "no-store"

                padding = 8192 - len(signed(now, padding=0))
                boundary = signed(now, padding=padding)
                assert len(boundary) == 8192 and len(boundary.encode()) > 8192
                compare("start", boundary, 200)
                compare("start", signed(now - 400, padding=padding), 403)
                compare("start", boundary[:-1] + "Я", 403)
                oversized = {"init_data": signed(now, padding=padding + 1)}
                old = oracle.post("/garmin-auth/start", json=oversized)
                new = native.post("/garmin-auth/start", json=oversized)
                assert old.status_code == new.status_code == 422
                assert new.json() == {"detail": "Invalid arguments"}
                assert new.headers["cache-control"] == "no-store"

                status(engine, "reauth_required", "AccountMismatch")
                compare("start", current, 409)
                status(engine, "reauth_required", "AuthenticationRequired")
                compare("complete", current, 400)
                compare("complete", stale, 400)
                assert not settings.token_dir.exists()
        finally:
            server.terminate()
            assert server.wait(timeout=5) == 0
    print("Matched the pinned Mini App page and 17 signed request outcomes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(engine, args.binary.resolve(strict=True), Path(directory))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
