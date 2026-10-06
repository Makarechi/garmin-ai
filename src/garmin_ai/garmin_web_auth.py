"""Short-lived Telegram Mini App flow for restoring Garmin tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from threading import Lock
from urllib.parse import parse_qsl

import httpx
from garminconnect import Garmin, GarminConnectAuthenticationError
from sqlalchemy import text

from garmin_ai.accounts import verify_setup_account
from garmin_ai.archive import private_directory
from garmin_ai.garmin import GarminReader
from garmin_ai.integration import resume_after_login


def validate_init_data(raw: str, bot_token: str, owner_id: int, *, now: int | None = None) -> None:
    """Reject forged, stale or non-owner Mini App requests."""
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
    if auth_date > instant + 30 or instant - auth_date > 300:
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

    def _publish(self, client):
        token_dir = private_directory(self.settings.token_dir)
        reader = GarminReader(client)

        def publish():
            client.client.dump(str(token_dir.resolve()))
            with (token_dir / "garmin_tokens.json").open("rb") as tokens:
                os.fsync(tokens.fileno())

        fingerprint = reader.account_fingerprint()
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
            guard.execute(text("SELECT pg_advisory_lock(72104622)"))
            try:
                verify_setup_account(
                    self.engine,
                    fingerprint,
                    archive_root=self.settings.data_dir / "raw",
                    before_commit=publish,
                )
                resume_after_login(self.settings)
            finally:
                guard.execute(text("SELECT pg_advisory_unlock(72104622)"))

    def start(self):
        with self._lock:
            if not self.settings.garmin_email or not self.settings.garmin_password_secret_version:
                raise RuntimeError("Garmin web login is not configured")
            now = time.monotonic()
            if self._last_start is not None and now - self._last_start < 60:
                raise ValueError("Code was requested recently")
            self._last_start = now
            self._client = None
            self._attempts = 0
            password = read_garmin_password(self.settings.garmin_password_secret_version)
            client = Garmin(
                email=self.settings.garmin_email,
                password=password,
                return_on_mfa=True,
            )
            # Never reuse an ambient token cache instead of the configured password.
            ambient = os.environ.pop("GARMINTOKENS", None)
            try:
                status, _ = client.login()
            finally:
                if ambient is not None:
                    os.environ["GARMINTOKENS"] = ambient
                client.password = None
            if status == "needs_mfa":
                self._client = client
                self._deadline = time.monotonic() + 300
                return "code_required"
            self._publish(client)
            return "restored"

    def complete(self, code: str):
        if not code or len(code) > 20 or not code.isascii() or not code.isalnum():
            raise ValueError("Invalid code")
        with self._lock:
            if self._client is None or time.monotonic() >= self._deadline:
                self._client = None
                raise ValueError("Login session expired")
            self._attempts += 1
            if self._attempts > 3:
                self._client = None
                raise ValueError("Too many attempts")
            client = self._client
            try:
                client.resume_login({}, code)
            except GarminConnectAuthenticationError:
                raise ValueError("Invalid code") from None
            self._publish(client)
            self._client = None
            return "restored"


FORM_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход в Garmin</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font:16px system-ui;max-width:420px;margin:40px auto;padding:0 20px;color:#18232b}button,input{font:inherit;width:100%;box-sizing:border-box;padding:12px;margin:8px 0}button{background:#007f74;color:white;border:0;border-radius:8px}#message{min-height:2em}</style></head>
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
 if(result.status==='code_required'){document.getElementById('codeForm').hidden=false;document.getElementById('start').hidden=true;message.textContent='Письмо отправлено. Введите код.';}
 else{document.getElementById('codeForm').hidden=true;message.textContent='Доступ к Garmin восстановлен. Можно закрыть форму.';}
 }catch(_){message.textContent='Нет соединения с сервером. Попробуйте снова.';}
}
document.getElementById('start').onclick=()=>request('/garmin-auth/start','');
document.getElementById('codeForm').onsubmit=e=>{e.preventDefault();request('/garmin-auth/complete',document.getElementById('code').value.trim());};
</script></body></html>"""
