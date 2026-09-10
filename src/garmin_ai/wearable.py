"""Write-only wearable uploads; device time and content need owner confirmation."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from garmin_ai.events import (
    Caffeine,
    Conflict,
    EventInput,
    Medication,
    StrictModel,
    create_event,
    lock_writes,
)
from garmin_ai.models import AppState


class WearableMark(StrictModel):
    id: UUID
    device_time: AwareDatetime
    timezone: str = Field(min_length=1, max_length=100)
    clock_uncertainty_seconds: int | None = Field(default=None, ge=0, le=31536000, strict=True)
    payload: Annotated[Caffeine | Medication, Field(discriminator="type")]

    @model_validator(mode="after")
    def valid_mark(self):
        if isinstance(self.payload, Medication) and self.payload.reason_event_id is not None:
            raise ValueError("Wearable marks cannot reference existing diary records")
        # Apply the same timezone and payload constraints before any batch write.
        EventInput(start=self.device_time, timezone=self.timezone, payload=self.payload)
        return self


class WearableBatch(StrictModel):
    marks: list[WearableMark] = Field(min_length=1, max_length=20)


def accept_batch(session, device_id, batch, *, now=None):
    now = now or datetime.now(UTC)
    lock_writes(session)
    acknowledgements = []
    with session.begin_nested():
        for mark in batch.marks:
            digest = hashlib.sha256(
                json.dumps(
                    mark.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            key = f"wearable-receipt:{device_id}:{mark.id}"
            receipt = session.get(AppState, key)
            if receipt:
                if receipt.value["hash"] != digest:
                    raise Conflict("Wearable mark ID was already used with different content")
            else:
                event = create_event(
                    session,
                    EventInput(
                        start=mark.device_time,
                        timezone=mark.timezone,
                        source="wearable",
                        status="needs_confirmation",
                        confidence=0,
                        payload=mark.payload,
                        original_text="Отметка с часов: время и содержание требуют подтверждения владельца.",
                    ),
                    actor=f"wearable:{device_id}",
                    idempotency_key=f"wearable:{device_id}:{mark.id}",
                )
                session.add(
                    AppState(
                        key=key,
                        value={
                            "hash": digest,
                            "event_id": str(event.id),
                            "received_at": now.isoformat(),
                            "device_time": mark.device_time.isoformat(),
                            "timezone": mark.timezone,
                            "clock_uncertainty_seconds": mark.clock_uncertainty_seconds,
                            "time_status": "unverified_device_clock",
                        },
                    )
                )
                session.flush()
            # ACK is acceptance of the immutable upload, never a diary-state read.
            acknowledgements.append({"id": str(mark.id), "accepted": True})
    return {"acknowledgements": acknowledgements}
