"""Trusted canonical envelope metadata for event facts."""

from datetime import UTC, datetime

from sqlalchemy import func, select, text

from garmin_ai.models import AppState, Event, EventDefinitionVersion

CANONICAL_VALIDATION_KEY = "registry:canonical:validated"

LEGACY_EVENT_SOURCES = frozenset(
    {"manual", "telegram_text", "telegram_button", "telegram_voice", "mcp", "inferred", "wearable"}
)


def time_precision(topology: str) -> str:
    if topology == "point":
        return "instant"
    if topology in {"bounded_interval", "open_interval"}:
        return "interval"
    return "unknown"


def provenance_values(source: str, status: str, *, topology: str, actor: str | None = None) -> dict:
    """Derive provenance only from authenticated server context and validated input."""
    if actor is None:
        channel = (
            "telegram"
            if source.startswith("telegram_")
            else "wearable"
            if source == "wearable"
            else "mcp"
            if source == "mcp"
            else "local"
        )
    else:
        channel = (
            "telegram"
            if actor.startswith("telegram:")
            else "wearable"
            if actor.startswith("wearable:")
            else "mcp"
            if actor == "mcp"
            else "api"
            if actor == "api"
            else "local"
        )
    if channel == "wearable":
        assertion_kind, producer, transport, author = (
            "device_measurement",
            "wearable",
            "connector",
            None,
        )
    elif source == "inferred" and actor in {None, "system"}:
        assertion_kind, producer, transport, author = (
            "inferred",
            "system",
            channel if channel != "local" else None,
            None,
        )
    elif channel == "telegram":
        assertion_kind, producer, transport, author = (
            "user_report",
            "telegram",
            source if source.startswith("telegram_") else "telegram",
            "owner",
        )
    elif channel in {"mcp", "api"}:
        assertion_kind, producer, transport, author = (
            "user_report",
            "owner",
            channel,
            "owner",
        )
    else:
        assertion_kind, producer, transport, author = "user_report", "owner", source, "owner"
    validation = (
        "needs_confirmation"
        if status in {"inferred", "needs_confirmation"}
        else "trusted"
        if assertion_kind == "device_measurement"
        else "schema_validated"
    )
    now = datetime.now(UTC)
    return {
        "envelope_version": 1,
        "time_precision": time_precision(topology),
        "assertion_kind": assertion_kind,
        "producer": producer,
        "transport": transport,
        "author": author,
        "evidence_refs": [],
        "validation_status": validation,
        "recorded_at": now,
        "ingested_at": now,
    }


def backfill_canonical_events(session) -> int:
    """Validate a completed additive backfill without changing diary history."""
    missing = session.scalar(
        select(func.count())
        .select_from(Event)
        .where(
            Event.definition_version_id.is_(None),
        )
    )
    if missing:
        raise ValueError(f"Canonical event backfill has {missing} unresolved definitions")
    invalid_reference = session.scalar(
        select(func.count())
        .select_from(Event)
        .outerjoin(
            EventDefinitionVersion,
            EventDefinitionVersion.id == Event.definition_version_id,
        )
        .where(EventDefinitionVersion.id.is_(None))
    )
    if invalid_reference:
        raise ValueError("Canonical event backfill contains invalid definition references")
    return session.scalar(select(func.count()).select_from(Event)) or 0


def backfill_canonical_events_if_needed(session) -> int:
    if session.get(AppState, CANONICAL_VALIDATION_KEY, populate_existing=True) is not None:
        return 0
    session.execute(text("SELECT pg_advisory_xact_lock(72104630)"))
    if session.get(AppState, CANONICAL_VALIDATION_KEY, populate_existing=True) is not None:
        return 0
    count = backfill_canonical_events(session)
    session.add(AppState(key=CANONICAL_VALIDATION_KEY, value={"validated": True}))
    return count


def canonical_envelope(row) -> dict:
    return {
        "version": row.envelope_version,
        "definition_version_id": str(row.definition_version_id)
        if row.definition_version_id
        else None,
        "observed": {
            "start": row.start.isoformat(),
            "end": row.end.isoformat() if row.end else None,
            "timezone": row.timezone,
            "topology": row.topology,
            "precision": row.time_precision,
        },
        "recorded_at": row.recorded_at.isoformat(),
        "ingested_at": row.ingested_at.isoformat(),
        "provenance": {
            "assertion_kind": row.assertion_kind,
            "producer": row.producer,
            "transport": row.transport,
            "author": row.author,
            "evidence_refs": row.evidence_refs,
            "confidence": row.confidence,
            "validation_status": row.validation_status,
        },
    }
