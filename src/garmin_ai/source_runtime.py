"""Run explicitly selected source extensions into private raw provenance storage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert

from garmin_ai.db import transaction
from garmin_ai.events import lock_writes
from garmin_ai.integrations import configured_instances, default_registry
from garmin_ai.models import AppState, SourcePayload
from garmin_ai.onboarding import source_instance_selected
from garmin_ai.source_contracts import (
    SourceCapabilities,
    SourcePage,
    record_overlaps_window,
)

ENDPOINT = "source-record-v1"
WINDOW_DAYS = 7
MAX_RECORD_BYTES = 64_000
MAX_CURSOR_HISTORY = 1000


def configured_source_plugins(settings, registry=None) -> tuple[str, ...]:
    registry = registry or default_registry(settings)
    selected = []
    for instance in configured_instances(settings):
        if not instance.enabled or instance.kind != "source":
            continue
        try:
            descriptor = registry.descriptor("source", instance.provider)
            if (
                descriptor.plugin_factory is not None
                and registry.status(instance, settings).available
            ):
                selected.append(instance.id)
        except Exception:
            continue
    return tuple(selected)


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _cursor_digest(cursor: str) -> str:
    return hashlib.sha256(cursor.encode()).hexdigest()


def poll_source_instance(
    engine,
    settings,
    instance_id: str,
    *,
    now: datetime | None = None,
    max_pages: int = 10,
) -> dict:
    """Persist bounded pages and commit each cursor only with its records."""

    now = now or datetime.now(UTC)
    if now.tzinfo is None or max_pages < 1:
        raise ValueError("Source poll needs aware time and a positive page budget")
    instance = next(
        (
            item
            for item in configured_instances(settings)
            if item.id == instance_id and item.enabled and item.kind == "source"
        ),
        None,
    )
    if instance is None:
        return {"status": "disabled", "records": 0, "pages": 0}
    registry = default_registry(settings)
    descriptor = registry.descriptor("source", instance.provider)
    if descriptor.plugin_factory is None or not registry.status(instance, settings).available:
        return {"status": "unavailable", "records": 0, "pages": 0}
    cursor_key = f"source-plugin:cursor:{instance.id}"
    with transaction(engine) as session:
        if not source_instance_selected(session, instance.id):
            return {"status": "disabled", "records": 0, "pages": 0}
        state = session.get(AppState, cursor_key)
        snapshot = dict(state.value) if state is not None else None
    if snapshot and snapshot.get("retry_after"):
        if datetime.fromisoformat(snapshot["retry_after"]) > now:
            return {"status": "deferred", "records": 0, "pages": 0}
    if snapshot and snapshot.get("next_cursor") is not None:
        start = datetime.fromisoformat(snapshot["window_start"])
        end = datetime.fromisoformat(snapshot["window_end"])
        cursor = snapshot["next_cursor"]
    else:
        start, end, cursor = now - timedelta(days=WINDOW_DAYS), now, None

    adapter = registry.create(instance, settings)
    try:
        declared = adapter.capabilities
        if not isinstance(declared, SourceCapabilities):
            raise ValueError("Source adapter lacks observation capability")
        capabilities = SourceCapabilities.model_validate(declared.model_dump(mode="python"))
        if not capabilities.observations:
            raise ValueError("Source adapter lacks observation capability")
        limit = min(100, capabilities.max_page_size)
        seen_cursors = set(snapshot.get("seen_cursor_hashes", [])) if cursor is not None else set()
        if cursor is not None:
            seen_cursors.add(_cursor_digest(cursor))
        records = pages = 0
        for _ in range(max_pages):
            returned = adapter.read_page(start=start, end=end, cursor=cursor, limit=limit)
            if not isinstance(returned, SourcePage):
                raise ValueError("Source adapter returned an invalid page type")
            page = SourcePage.model_validate(returned.model_dump(mode="python"))
            if page.instance_id != instance.id or len(page.records) > limit:
                raise ValueError("Source returned a different instance or oversized page")
            if page.page_kind == "complete_interval_snapshot" and cursor is not None:
                raise ValueError("Complete snapshot cannot follow a pagination cursor")
            next_cursor_digest = (
                _cursor_digest(page.next_cursor) if page.next_cursor is not None else None
            )
            if next_cursor_digest is not None and next_cursor_digest in seen_cursors:
                raise ValueError("Source repeated a cursor")
            if next_cursor_digest is not None and len(seen_cursors) >= MAX_CURSOR_HISTORY:
                raise ValueError("Source exceeded the cursor history limit")
            if page.next_cursor is not None and not capabilities.cursor:
                raise ValueError("Source returned a cursor without cursor capability")
            if capabilities.time_semantics == "interval":
                if any(record.effective_end is None for record in page.records):
                    raise ValueError("Interval source omitted an end")
            if any(
                not record_overlaps_window(record, capabilities.time_semantics, start, end)
                for record in page.records
            ):
                raise ValueError("Source returned an out-of-window record")
            if not capabilities.deletions and any(
                record.operation == "delete" for record in page.records
            ):
                raise ValueError("Source returned undeclared deletions")

            with transaction(engine) as session:
                lock_writes(session)
                if not source_instance_selected(session, instance.id):
                    return {"status": "disabled", "records": records, "pages": pages}
                stored = session.get(AppState, cursor_key, populate_existing=True)
                if (dict(stored.value) if stored is not None else None) != snapshot:
                    return {"status": "stale", "records": records, "pages": pages}
                for record in page.records:
                    value = record.model_dump(mode="json")
                    if (
                        len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
                        > MAX_RECORD_BYTES
                    ):
                        raise ValueError("Source record exceeds the private raw size limit")
                    digest = _digest(value)
                    identity = hashlib.sha256(record.source_record_id.encode()).hexdigest()
                    record_key = f"source-plugin:record:{instance.id}:{identity}"
                    prior = session.get(AppState, record_key)
                    if (
                        prior is not None
                        and prior.value.get("hash") != digest
                        and record.operation == "upsert"
                        and not capabilities.corrections
                    ):
                        raise ValueError("Source changed a record without correction capability")
                    session.execute(
                        insert(SourcePayload)
                        .values(
                            source=f"plugin:{instance.id}",
                            endpoint=ENDPOINT,
                            source_key=record.source_record_id,
                            payload_hash=digest,
                            payload=value,
                            archive_key=f"inline:{digest}",
                            fetched_at=page.fetched_at,
                            source_updated_at=record.observed_at,
                            parser_version=1,
                            status="raw_only",
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                SourcePayload.source,
                                SourcePayload.endpoint,
                                SourcePayload.source_key,
                                SourcePayload.payload_hash,
                            ]
                        )
                    )
                    if prior is None:
                        session.add(
                            AppState(
                                key=record_key,
                                value={"hash": digest, "operation": record.operation},
                            )
                        )
                    else:
                        prior.value = {"hash": digest, "operation": record.operation}
                snapshot = {
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "next_cursor": page.next_cursor,
                    "seen_cursor_hashes": (
                        sorted(seen_cursors | {next_cursor_digest})
                        if next_cursor_digest is not None
                        else []
                    ),
                    "retry_after": page.retry_after.isoformat() if page.retry_after else None,
                    "last_completed_at": now.isoformat() if page.next_cursor is None else None,
                }
                if stored is None:
                    session.add(AppState(key=cursor_key, value=snapshot))
                else:
                    stored.value = snapshot
            records += len(page.records)
            pages += 1
            if page.next_cursor is None:
                return {"status": "complete", "records": records, "pages": pages}
            cursor = page.next_cursor
            if next_cursor_digest is not None:
                seen_cursors.add(next_cursor_digest)
            if page.retry_after is not None and page.retry_after > now:
                return {"status": "deferred", "records": records, "pages": pages}
        return {"status": "partial", "records": records, "pages": pages}
    finally:
        adapter.close()
