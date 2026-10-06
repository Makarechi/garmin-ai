"""Short-lived Telegram Mini App flow for restoring Garmin tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import UTC, datetime
from threading import Lock, Timer
from urllib.parse import parse_qsl

import httpx
from garminconnect import Garmin, GarminConnectAuthenticationError

from garmin_ai.accounts import AccountEnrollmentRequired, AccountError, existing_account
from garmin_ai.archive import fsync_directory, private_directory
from garmin_ai.db import backup_token_guard, transaction
from garmin_ai.garmin import GarminReader
from garmin_ai.integration import record


def validate_init_data(
    raw: str, bot_token: str, owner_id: int, *, now: int | None = None, max_age: int | None = 300
) -> None:
    """Reject forged or non-owner Mini App requests; optionally enforce freshness."""
    if not raw or len(raw) > 8192 or not bot_token or not owner_id:
        raise ValueError("Invalid Telegram session")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    if len(values) != len(pairs) or "hash" not in values:
        raise ValueError("Invalid Telegram session")
    expected = hmac.new(
        hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest(),
        "\n".join(
            f"{key}={value}" for key, value in sorted(values.items()) if key != "hash"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(values["hash"], expected):
        raise ValueError("Invalid Telegram session")
    instant = int(time.time()) if now is None else now
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Invalid Telegram session") from exc
    if not isinstance(user, dict) or user.get("id") != owner_id:
        raise ValueError("Invalid Telegram owner")
    if auth_date > instant + 30 or (max_age is not None and instant - auth_date > max_age):
        raise ValueError("Telegram session expired")


def read_garmin_password(version: str) -> str:
    """Read one pinned Secret Manager version using the VM's service identity."""
    if not re.fullmatch(
        r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+/versions/[1-9][0-9]*",
        version,
    ):
        raise RuntimeError("Garmin password secret is not configured")
    token_response = httpx.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
        timeout=5,
    )
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]
    secret_response = httpx.get(
        f"https://secretmanager.googleapis.com/v1/{version}:access",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    secret_response.raise_for_status()
    password = base64.b64decode(secret_response.json()["payload"]["data"], validate=True).decode()
    if not password:
        raise RuntimeError("Garmin password secret is empty")
    return password


class GarminWebAuth:
    def __init__(self, settings, engine):
        self.settings = settings
        self.engine = engine
        self._lock = Lock()
        self._client = None
        self._deadline = 0.0
        self._attempts = 0
        self._last_start = None
        self._authenticated = False
        self._timer = None
        self._generation = 0
        self._mfa_state = None
        self._session_digest = None

    def _clear_client(self):
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._client = None
        self._deadline = 0.0
        self._attempts = 0
        self._authenticated = False
        self._mfa_state = None
        self._session_digest = None

    def _expire(self, generation):
        with self._lock:
            if generation == self._generation:
                self._clear_client()

    def _publish(self, client):
        token_dir = private_directory(self.settings.token_dir)
        reader = GarminReader(client)

        def publish():
            client.client.dump(str(token_dir.resolve()))
            with (token_dir / "garmin_tokens.json").open("rb") as tokens:
                os.fsync(tokens.fileno())
            fsync_directory(token_dir)

        try:
            fingerprint = reader.account_fingerprint()
            with backup_token_guard(self.engine), transaction(self.engine) as session:
                # A web login may restore an established owner, but may not enroll one.
                if existing_account(session, fingerprint) is None:
                    raise AccountEnrollmentRequired("Enroll the Garmin owner locally")
                publish()
                record(session, "active", datetime.now(UTC))
        except AccountError as exc:
            from garmin_ai.runtime import enqueue_connection_notice

            now = datetime.now(UTC)
            with transaction(self.engine) as session:
                record(session, "reauth_required", now, reason=type(exc).__name__, failure=True)
                enqueue_connection_notice(session, exc, now)
            raise

    def start(self, init_data: str):
        with self._lock:
            if not self.settings.garmin_email or not self.settings.garmin_password_secret_version:
                raise RuntimeError("Garmin web login is not configured")
            now = time.monotonic()
            if self._last_start is not None and now - self._last_start < 60:
                raise ValueError("Code was requested recently")
            self._last_start = now
            self._clear_client()
            password = read_garmin_password(self.settings.garmin_password_secret_version)
            client = Garmin(
                email=self.settings.garmin_email,
                password=password,
                return_on_mfa=True,
            )
            # Never reuse an ambient token cache instead of the configured password.
            ambient = os.environ.pop("GARMINTOKENS", None)
            try:
                status, continuation = client.login()
            finally:
                if ambient is not None:
                    os.environ["GARMINTOKENS"] = ambient
                client.password = None
            if status == "needs_mfa":
                self._client = client
                self._deadline = time.monotonic() + 300
                # The pinned client keeps MFA state internally and currently returns None.
                self._mfa_state = continuation or {}
                self._session_digest = hashlib.sha256(init_data.encode()).digest()
                generation = self._generation
                self._timer = Timer(300, self._expire, args=(generation,))
                self._timer.daemon = True
                self._timer.start()
                return "code_required"
            self._publish(client)
            return "restored"

    def complete(self, code: str, init_data: str):
        if not code or len(code) > 20 or not code.isascii() or not code.isalnum():
            raise ValueError("Invalid code")
        with self._lock:
            if self._client is None or time.monotonic() >= self._deadline:
                self._clear_client()
                raise ValueError("Login session expired")
            if not hmac.compare_digest(
                self._session_digest, hashlib.sha256(init_data.encode()).digest()
            ):
                raise ValueError("Login session does not match")
            client = self._client
            try:
                if not self._authenticated:
                    self._attempts += 1
                    if self._attempts > 3:
                        self._clear_client()
                        raise ValueError("Too many attempts")
                    client.resume_login(self._mfa_state, code)
                    self._authenticated = True
            except GarminConnectAuthenticationError:
                if self._attempts >= 3:
                    self._clear_client()
                raise ValueError("Invalid code") from None
            self._publish(client)
            self._clear_client()
            return "restored"


FORM_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход в Garmin</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font:16px system-ui;max-width:420px;margin:40px auto;padding:0 20px;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#18232b)}button,input{font:inherit;width:100%;box-sizing:border-box;padding:12px;margin:8px 0}button{background:#007f74;color:white;border:0;border-radius:8px}button:disabled{opacity:.5}#message{min-height:2em}</style></head>
<body><h1>Восстановить Garmin</h1><p>Введите код, который Garmin отправит на почту. Пароль в этой форме не нужен.</p>
<button id="start">Отправить код</button><form id="codeForm" hidden><label for="code">Код из письма</label><input id="code" autocomplete="one-time-code" inputmode="numeric" required maxlength="20"><button>Подтвердить</button></form><p id="message" role="status"></p>
<script>
const app=window.Telegram&&window.Telegram.WebApp;
const message=document.getElementById('message');
if(!app||!app.initData){message.textContent='Откройте форму кнопкой в личном чате с ботом.';document.getElementById('start').disabled=true;}else{app.ready();}
async function request(path,code){
 message.textContent='Проверяю…';
 try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({init_data:app.initData,code})});
 const result=await response.json();
 if(!response.ok){message.textContent=result.detail||'Не удалось завершить вход. Попробуйте снова.';return;}
 if(result.status==='code_required'){document.getElementById('codeForm').hidden=false;document.getElementById('start').textContent='Отправить новый код';message.textContent='Письмо отправлено. Введите код.';}
 else{document.getElementById('codeForm').hidden=true;document.getElementById('start').hidden=true;message.textContent='Доступ к Garmin восстановлен. Можно закрыть форму.';}
 }catch(_){message.textContent='Нет соединения с сервером. Попробуйте снова.';}
}
document.getElementById('start').onclick=()=>request('/garmin-auth/start','');
document.getElementById('codeForm').onsubmit=e=>{e.preventDefault();request('/garmin-auth/complete',document.getElementById('code').value.trim());};
</script></body></html>"""
