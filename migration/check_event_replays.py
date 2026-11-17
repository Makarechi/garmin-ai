"""Generate synthetic idempotency outcomes from the pinned diary implementation."""

import json
import sys
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

from garmin_ai.events import Conflict, EventInput, event_values, replay_matches

BASE = {
    "start": "2026-10-25T02:30:00+02:00",
    "timezone": "Europe/Bratislava",
}
EVENT = SimpleNamespace(id=UUID("9b89716e-7197-472f-9ccb-6ceed2f405b5"))


class AuditedCreation:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def scalar(self, _statement):
        return SimpleNamespace(after=self.snapshot) if self.snapshot is not None else None


def observe(report, audited):
    event = EventInput.model_validate(report)
    try:
        replay_matches(AuditedCreation(audited), EVENT, event_values(event))
    except Conflict as error:
        return "unavailable" if "audit unavailable" in str(error).lower() else "changed"
    return "matched"


def cases():
    for payload in (
        {"type": "caffeine", "beverage": "Synthetic", "caffeine_mg_estimate": 100},
        {"type": "medication"},
        {"type": "note", "description": "Synthetic", "amount": 9007199254740992.0},
        {"type": "activity_effort", "activity_id": "synthetic", "perceived_exertion": 3},
    ):
        report = {**BASE, "payload": payload}
        event = EventInput.model_validate(report)
        snapshot = json.loads(json.dumps(event_values(event), default=lambda value: value.isoformat()))
        yield report, snapshot
        yield report, None
        for field, different in (
            ("start", "2026-10-25T02:30:00+01:00"),
            ("timezone", "UTC"),
            ("confidence", 0),
            ("source", "mcp"),
            ("status", "inferred"),
            ("original_text", "Synthetic edit"),
        ):
            yield report, {**snapshot, field: different}
        yield report, {**snapshot, "start": "2026-10-25T00:30:00Z"}
        yield report, {**snapshot, "confidence": True}
        yield report, {**snapshot, "revision": 17, "deleted": True}
        if payload["type"] == "caffeine":
            yield report, {**snapshot, "payload": payload}
            yield report, {**snapshot, "payload": {**payload, "caffeine_mg_estimate": 101}}
        if payload["type"] == "medication":
            candidate = deepcopy(snapshot)
            candidate["payload"]["dose"] = 1.0
            yield report, candidate
        if payload["type"] == "note":
            for amount in (9007199254740992, 9007199254740993):
                yield report, {**snapshot, "payload": {**snapshot["payload"], "amount": amount}}


if __name__ == "__main__":
    for report, snapshot in cases():
        outcome = observe(report, snapshot)
        print(json.dumps([report, snapshot, outcome]), file=sys.stdout)
