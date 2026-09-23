"""Signed, expiring, context-bound, single-use action references."""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from garmin_ai.models import AppState, Person

PREFIX = "action-token:"
SIGNING_PREFIX = "action-signing-key:"


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
    encoded = _encode(secrets.token_bytes(18))
    signature = _encode(hmac.new(signing_key, encoded.encode(), hashlib.sha256).digest()[:24])
    token = encoded + "." + signature
    session.add(
        AppState(
            key=PREFIX + hashlib.sha256(token.encode()).hexdigest(),
            value={
                "owner_id": str(owner_id),
                "conversation_id": str(conversation_id),
                "action_id": action_id,
                "revision": revision,
                "expires_at": expires_at.isoformat(),
            },
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
        expected = _encode(
            hmac.new(signing_key, encoded.encode(), hashlib.sha256).digest()[:24]
        )
        if not hmac.compare_digest(supplied, expected):
            return None
        state = session.scalar(
            select(AppState)
            .where(AppState.key == PREFIX + hashlib.sha256(token.encode()).hexdigest())
            .with_for_update()
        )
        payload = state.value if state is not None else None
        if (
            not isinstance(payload, dict)
            or payload["owner_id"] != str(owner_id)
            or payload["conversation_id"] != str(conversation_id)
            or payload["revision"] != revision
            or datetime.fromisoformat(payload["expires_at"]) <= now
        ):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    session.delete(state)
    session.flush()
    return payload["action_id"]


def owner_action_signing_key(session, owner_id: UUID) -> bytes:
    """Load or create the per-owner action signing key under the owner row lock."""

    if session.get(Person, owner_id, with_for_update=True) is None:
        raise LookupError("Action token owner not found")
    identity = SIGNING_PREFIX + str(owner_id)
    state = session.get(AppState, identity)
    if state is None:
        state = AppState(key=identity, value={"key": _encode(secrets.token_bytes(32))})
        session.add(state)
        session.flush()
    try:
        key = _decode(state.value["key"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Action signing key is invalid") from None
    if len(key) < 32:
        raise ValueError("Action signing key is invalid")
    return key
