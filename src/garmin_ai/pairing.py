"""First-owner Telegram pairing controlled from the local instance console."""

import hmac
import io
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from telegram import Bot

from garmin_ai.archive import atomic_private_write, has_path_redirect
from garmin_ai.config import Settings
from garmin_ai.storage_files import standalone_files


class PairingSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, **sources):
        return (sources["init_settings"],)


def load_pairing(path):
    path = Path(path)
    if has_path_redirect(path) or not path.is_file():
        raise ValueError("Pairing requires an existing regular environment file")
    original = path.read_bytes()
    values = dotenv_values(stream=io.StringIO(original.decode("utf-8")), interpolate=False)
    if values.get("GA_TELEGRAM_USER_ID") not in (None, "", "0"):
        raise ValueError("Telegram owner is already configured; pairing cannot replace it")
    token = values.get("GA_TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("replace-with-"):
        raise ValueError("Configure the Telegram bot token locally before pairing")
    selected = {"telegram_bot_token": token, "telegram_user_id": 0}
    defaults = {
        "data_dir": "data",
        "token_dir": "tokens/garmin",
        "backup_dir": "backups",
        "lock_dir": ".state",
    }
    for key in defaults:
        value = values.get("GA_" + key.upper()) or defaults[key]
        selected[key] = (path.parent / value).absolute()
    if values.get("GA_DATABASE_URL"):
        selected["database_url"] = values["GA_DATABASE_URL"]
    return original, PairingSettings(**selected)


def save_owner(path, original, owner):
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        raise ValueError("Invalid private Telegram owner")
    current, _ = load_pairing(path)
    if current != original:
        raise ValueError("Environment file changed during pairing; restart pairing")
    content = original.decode("utf-8")
    line = f"GA_TELEGRAM_USER_ID='{owner}'\n"
    bindings = list(parse_stream(io.StringIO(content)))
    if any(binding.error for binding in bindings):
        raise ValueError("Environment file contains invalid syntax")
    found = any(binding.key == "GA_TELEGRAM_USER_ID" for binding in bindings)
    content = "".join(
        line if binding.key == "GA_TELEGRAM_USER_ID" else binding.original.string
        for binding in bindings
    )
    if not found:
        content += ("" if content.endswith("\n") else "\n") + line
    atomic_private_write(Path(path), content.encode("utf-8"), preserve_parent_mode=True)


async def discover_owner(bot, code, issued_at, *, timeout=180, clock=time.monotonic):
    deadline = clock() + timeout
    offset = None
    while clock() < deadline:
        updates = await bot.get_updates(
            offset=offset,
            timeout=min(10, max(1, int(deadline - clock()))),
            allowed_updates=["message"],
        )
        if clock() >= deadline:
            break
        for update in updates:
            offset = max(offset or 0, update.update_id + 1)
            message = update.message
            if (
                message is None
                or message.chat.type != "private"
                or message.from_user is None
                or message.from_user.is_bot
                or message.from_user.id != message.chat.id
                or message.forward_origin is not None
            ):
                continue
            candidate = (message.text or "").strip()
            if hmac.compare_digest(candidate.encode(), ("/pair " + code).encode()):
                # Confirm only through this matching update; later updates remain queued.
                await bot.get_updates(
                    offset=update.update_id + 1, timeout=0, allowed_updates=["message"]
                )
                return message.from_user.id
    raise TimeoutError("Pairing expired without a matching private message")


async def pair_telegram(path):
    original, settings = load_pairing(path)
    with standalone_files(settings):
        async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
            if (await bot.get_webhook_info()).url:
                raise ValueError("Pairing requires an unconfigured bot without a webhook")
            identity = await bot.get_me()
            code = secrets.token_urlsafe(24)
            # Telegram timestamps have second precision. The random code prevents replay.
            issued_at = datetime.now(UTC).replace(microsecond=0)
            print(
                f"Open a private chat with @{identity.username} and send within 3 minutes: /pair {code}",
                flush=True,
            )
            owner = await discover_owner(bot, code, issued_at)
            save_owner(path, original, owner)
    print(
        "Telegram owner paired. Recreate the Compose worker with docker compose up -d --force-recreate worker (using this instance env file); for a host service, restart it with the updated environment."
    )
