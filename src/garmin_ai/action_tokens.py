"""Signed, expiring, context-bound, single-use action references."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID

from garmin_ai.models import AppState

PREFIX = "action-token:"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_action_token(
    session,
    signing_key: bytes,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    action_id: str,
    revision: int,
    expires_at: datetime,
) -> str:
    if len(signing_key) < 32 or expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise ValueError("Action signing key or expiry is invalid")
    payload = json.dumps(
        {
            "owner_id": str(owner_id),
            "conversation_id": str(conversation_id),
            "action_id": action_id,
            "revision": revision,
            "expires_at": expires_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = _encode(payload)
    signature = _encode(hmac.new(signing_key, encoded.encode(), hashlib.sha256).digest())
    token = encoded + "." + signature
    session.add(
        AppState(
            key=PREFIX + hashlib.sha256(token.encode()).hexdigest(),
            value={"issued": True},
        )
    )
    session.flush()
    return token


def consume_action_token(
    session,
    signing_key: bytes,
    token: str,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    revision: int,
    now: datetime,
) -> str | None:
    if len(signing_key) < 32 or now.tzinfo is None or len(token) > 1000:
        return None
    try:
        encoded, supplied = token.split(".", 1)
        expected = _encode(hmac.new(signing_key, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(_decode(encoded))
        if (
            payload["owner_id"] != str(owner_id)
            or payload["conversation_id"] != str(conversation_id)
            or payload["revision"] != revision
            or datetime.fromisoformat(payload["expires_at"]) <= now
        ):
            return None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    row = session.get(AppState, PREFIX + hashlib.sha256(token.encode()).hexdigest())
    if row is None:
        return None
    session.delete(row)
    session.flush()
    return payload["action_id"]
