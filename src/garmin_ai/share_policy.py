"""Explicit consent gates for sensitive tracker schemas and facts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import DateTime, cast, func, or_, select, text

from garmin_ai.events import StrictModel, lock_writes
from garmin_ai.models import AppState, Event, EventDefinition, EventDefinitionVersion, OutboxMessage
from garmin_ai.normalize import upsert

CONSENT_PREFIX = "tracker-consent:"


@contextmanager
def channel_consent_delivery_fence(engine):
    """Hold consent stable from final validation through provider send."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SELECT pg_advisory_lock_shared(72104631)"))
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock_shared(72104631)"))


@contextmanager
def model_consent_delivery_fence(engine):
    """Hold model sharing consent through voice transcription."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SELECT pg_advisory_lock_shared(72104632)"))
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock_shared(72104632)"))


def _channel_consent_write_fence(session):
    # Telegram delivery holds the replay fence before the consent fence.
    # Mutations must use that same order when forgetting retained context.
    lock_writes(session)
    session.execute(select(func.pg_advisory_xact_lock(72104631)))


def _model_consent_write_fence(session):
    lock_writes(session)
    session.execute(select(func.pg_advisory_xact_lock(72104632)))


def track_channel_share(session, version_id: UUID, categories: set[str]) -> None:
    """Keep only consent dependencies, never tracker payload, with a queued reply."""
    if session.info.get("channel_destination_instance_id") is None:
        return
    requirements = session.info.setdefault("channel_share_requirements", {})
    key = str(version_id)
    requirements[key] = sorted(set(requirements.get(key, [])) | categories)


class TrackerShareConsent(StrictModel):
    definition_id: UUID
    destination_kind: Literal["model", "channel"]
    destination_instance_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,199}$")
    categories: set[Literal["schema", "facts", "original_text"]] = Field(min_length=1)
    granted_at: AwareDatetime
    policy_revision: Literal[1] = 1


def _key(definition_id, kind, instance_id):
    return f"{CONSENT_PREFIX}{definition_id}:{kind}:{instance_id}"


def _forget_model_context(session):
    from garmin_ai.conversation import forget_conversation

    forget_conversation(session)


def _forget_channel_context(session, destination_instance_id):
    from garmin_ai.conversation import forget_channel_context

    forget_channel_context(session, destination_instance_id)


def _cancel_queued_channel_shares(session, definition_id, destination_instance_id):
    evidence_ref = f"definition:{definition_id}"
    for message in session.scalars(select(OutboxMessage).where(OutboxMessage.state == "queued")):
        destination = message.intent.get("channel_instance", {})
        actual_instance = f"{destination.get('channel')}:{destination.get('instance_id')}"
        if (
            evidence_ref in message.intent.get("evidence_refs", [])
            and actual_instance == destination_instance_id
        ):
            message.state = "cancelled"
            message.next_attempt_at = None


def grant_tracker_share(session, consent: TrackerShareConsent, *, authorized=False):
    if not authorized:
        raise PermissionError("Integration consent management permission required")
    consent = TrackerShareConsent.model_validate(consent)
    if consent.granted_at > datetime.now(UTC):
        raise ValueError("Tracker sharing consent cannot be granted in the future")
    if consent.destination_kind == "channel":
        _channel_consent_write_fence(session)
    else:
        _model_consent_write_fence(session)
    definition = session.get(EventDefinition, consent.definition_id)
    if definition is None or definition.namespace != "user":
        raise LookupError("Tracker definition not found")
    previous = session.get(
        AppState,
        _key(consent.definition_id, consent.destination_kind, consent.destination_instance_id),
    )
    if previous is not None:
        previous_consent = TrackerShareConsent.model_validate(previous.value)
        removed = previous_consent.categories - consent.categories
        if consent.destination_kind == "model" and removed & {"facts", "original_text"}:
            _forget_model_context(session)
        if consent.destination_kind == "channel" and removed & {"schema", "facts"}:
            _cancel_queued_channel_shares(
                session, consent.definition_id, consent.destination_instance_id
            )
            _forget_channel_context(session, consent.destination_instance_id)
    upsert(
        session,
        AppState,
        {
            "key": _key(
                consent.definition_id,
                consent.destination_kind,
                consent.destination_instance_id,
            ),
            "value": consent.model_dump(mode="json"),
        },
        ["key"],
    )
    return consent


def list_tracker_shares(session) -> list[TrackerShareConsent]:
    return [
        TrackerShareConsent.model_validate(row.value)
        for row in session.scalars(
            select(AppState).where(AppState.key.startswith(CONSENT_PREFIX)).order_by(AppState.key)
        )
    ]


def revoke_tracker_share(
    session,
    definition_id: UUID,
    destination_kind: Literal["model", "channel"],
    destination_instance_id: str,
    *,
    authorized=False,
) -> bool:
    if not authorized:
        raise PermissionError("Integration consent management permission required")
    if destination_kind == "channel":
        _channel_consent_write_fence(session)
    else:
        _model_consent_write_fence(session)
    row = session.get(
        AppState,
        _key(definition_id, destination_kind, destination_instance_id),
    )
    if row is None:
        return False
    session.delete(row)
    if destination_kind == "model":
        _forget_model_context(session)
    else:
        _cancel_queued_channel_shares(session, definition_id, destination_instance_id)
        _forget_channel_context(session, destination_instance_id)
    session.flush()
    return True


def sharing_allowed(
    session,
    definition_id: UUID,
    *,
    destination_kind: Literal["model", "channel"],
    destination_instance_id: str,
    categories: set[str],
) -> bool:
    definition = session.get(EventDefinition, definition_id)
    if definition is None:
        return False
    version = session.scalar(
        select(EventDefinitionVersion).where(
            EventDefinitionVersion.definition_id == definition.id,
            EventDefinitionVersion.version == definition.current_version,
        )
    )
    if version is None:
        return False
    if version.privacy != "sensitive" and "original_text" not in categories:
        return True
    row = session.get(
        AppState,
        _key(definition.id, destination_kind, destination_instance_id),
    )
    if row is None:
        return False
    consent = TrackerShareConsent.model_validate(row.value)
    return consent.granted_at <= datetime.now(UTC) and categories <= consent.categories


def version_sharing_allowed(
    session,
    version_id: UUID,
    *,
    destination_kind: Literal["model", "channel"],
    destination_instance_id: str,
    categories: set[str],
) -> bool:
    version = session.get(EventDefinitionVersion, version_id)
    if version is None:
        return False
    if version.privacy != "sensitive" and "original_text" not in categories:
        return True
    row = session.get(
        AppState,
        _key(version.definition_id, destination_kind, destination_instance_id),
        populate_existing=True,
    )
    if row is None:
        return False
    consent = TrackerShareConsent.model_validate(row.value)
    return consent.granted_at <= datetime.now(UTC) and categories <= consent.categories


def event_sharing_filter(
    *,
    destination_kind: Literal["model", "channel"],
    destination_instance_id: str,
    categories: set[str],
):
    """SQL predicate that applies sensitive-tracker consent before pagination."""

    consent = (
        select(AppState.key)
        .where(
            AppState.key
            == func.concat(
                CONSENT_PREFIX,
                EventDefinitionVersion.definition_id,
                f":{destination_kind}:{destination_instance_id}",
            ),
            AppState.value["categories"].contains(sorted(categories)),
            cast(AppState.value["granted_at"].as_string(), DateTime(timezone=True)) <= func.now(),
        )
        .correlate(EventDefinitionVersion)
        .exists()
    )
    custom_version_allowed = (
        select(EventDefinitionVersion.id)
        .where(
            EventDefinitionVersion.id == Event.definition_version_id,
            or_(EventDefinitionVersion.privacy != "sensitive", consent),
        )
        .correlate(Event)
        .exists()
    )
    return or_(Event.kind.not_like("user.%"), custom_version_allowed)
