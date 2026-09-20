"""Explicit consent gates for sensitive tracker schemas and facts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import select

from garmin_ai.events import StrictModel
from garmin_ai.models import AppState, EventDefinition, EventDefinitionVersion
from garmin_ai.normalize import upsert

CONSENT_PREFIX = "tracker-consent:"


class TrackerShareConsent(StrictModel):
    definition_id: UUID
    destination_kind: Literal["model", "channel"]
    destination_instance_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,199}$")
    categories: set[Literal["schema", "facts", "original_text"]] = Field(min_length=1)
    granted_at: AwareDatetime
    policy_revision: Literal[1] = 1


def _key(definition_id, kind, instance_id):
    return f"{CONSENT_PREFIX}{definition_id}:{kind}:{instance_id}"


def grant_tracker_share(session, consent: TrackerShareConsent, *, authorized=False):
    if not authorized:
        raise PermissionError("Integration consent management permission required")
    consent = TrackerShareConsent.model_validate(consent)
    definition = session.get(EventDefinition, consent.definition_id)
    if definition is None or definition.namespace != "user":
        raise LookupError("Tracker definition not found")
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
    return categories <= consent.categories


def version_sharing_allowed(
    session,
    version_id: UUID,
    *,
    destination_kind: Literal["model", "channel"],
    destination_instance_id: str,
    categories: set[str],
) -> bool:
    version = session.get(EventDefinitionVersion, version_id)
    return bool(
        version
        and sharing_allowed(
            session,
            version.definition_id,
            destination_kind=destination_kind,
            destination_instance_id=destination_instance_id,
            categories=categories,
        )
    )
