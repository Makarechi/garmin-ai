"""Capture decisions for local time evidence from the pinned Python interpreter."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.agent import Interpretation, interpret
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.i18n import translate

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def draft(start, timezone="Europe/Bratislava", **fields):
    return {"start": start, "timezone": timezone, "payload": {"type": "migraine"}, **fields}


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            target = create_event(
                session,
                EventInput(
                    start="2026-10-23T12:00:00+00:00",
                    timezone="UTC",
                    payload={"type": "note", "description": "synthetic target"},
                ),
                actor="owner",
            )
            original = draft("2026-10-25T02:15:00+02:00")
            later = draft("2026-10-25T02:15:00+01:00")

            def report(*events):
                return {"intent": "log", "confidence": 1, "events": events}

            correction = {
                "intent": "update",
                "confidence": 1,
                "target_event_id": str(target.id),
                "events": [
                    draft(
                        "2026-10-25T02:15:00+02:00",
                        payload={"type": "note", "description": "synthetic correction"},
                    )
                ],
            }
            pairs = [
                ("ordinary", "в полдень", report(draft("2026-10-24T12:00:00+02:00")), None),
                (
                    "point_end_is_removed",
                    "synthetic wellbeing report",
                    report(
                        draft(
                            "2026-10-24T12:00:00+02:00",
                            end="2026-10-24T10:00:00+00:00",
                            payload={"type": "wellbeing_observation", "energy": 6},
                        )
                    ),
                    None,
                ),
                ("unicode_clock_digits", "0٢:1٥+02:00", report(original), None),
                ("unicode_conflicting_occurrences", "02:15+02:00 и 0٢:1٥", report(original), None),
                ("unicode_offset_is_inconclusive", "02:15+٠٢:٠٠", report(original), None),
                ("wrong_zone_offset", "в 12:00", report(draft("2026-10-24T12:00:00+01:00")), None),
                ("spring_gap", "02:15+01:00", report(draft("2026-03-29T02:15:00+01:00")), None),
                ("no_fold_offset", "в 02:15", report(original), None),
                ("early_fold", "🧪 в 02:15 UTC+02:00", report(original), None),
                ("late_fold", "в 2:15+0100", report(later), None),
                ("wrong_explicit_offset", "в 02:15+01:00", report(original), None),
                ("same_time_twice", "02:15+02:00 и 02:15", report(original), None),
                ("same_offset_twice", "02:15+02:00 и 02:15+0200", report(original), None),
                (
                    "two_folds_unqualified",
                    "02:15+02:00 и 02:30",
                    report(original, draft("2026-10-25T02:30:00+02:00")),
                    None,
                ),
                (
                    "two_folds_qualified",
                    "02:15+02:00 и 02:30+02:00",
                    report(original, draft("2026-10-25T02:30:00+02:00")),
                    None,
                ),
                ("fold_seconds", "в 02:15:34.500 GMT+02:00", report(original), None),
                ("unrelated_number_prefix", "102:15+02:00", report(original), None),
                ("future_boundary", "чуть позже", report(draft("2026-10-26T13:05:00+01:00")), None),
                (
                    "future_over_boundary",
                    "позже",
                    report(draft("2026-10-26T13:05:00.000001+01:00")),
                    None,
                ),
                (
                    "future_end",
                    "позже",
                    report(draft("2026-10-26T12:00:00+01:00", end="2026-10-26T13:06:00+01:00")),
                    None,
                ),
                (
                    "unchanged_clock",
                    "измени описание",
                    {**correction, "changed_fields": ["payload.description"]},
                    "UTC",
                ),
                (
                    "correction_uses_stored_timezone",
                    "в 02:15+02:00",
                    {**correction, "changed_fields": ["start"]},
                    "UTC",
                ),
                (
                    "explicit_timezone_change",
                    "в 02:15+02:00",
                    {**correction, "changed_fields": ["start", "timezone"]},
                    "UTC",
                ),
            ]
            cases = []
            for name, source, response, target_zone in pairs:

                class Provider:
                    def structured(self, *_, response=response):
                        return Interpretation.model_validate(response)

                result = interpret(session, Provider(), source, Settings(), NOW)
                reason = next(
                    (
                        key
                        for key in ("timezone_mismatch", "ambiguous_time", "future_time")
                        if result.clarification == translate(f"agent.{key}", "ru")
                    ),
                    None,
                )
                assert result.intent == response["intent"] or reason, name
                cases.append(
                    {
                        "name": name,
                        "text": source,
                        "command": response,
                        "target_timezone": target_zone,
                        "reason": reason,
                    }
                )
            output.write_text(
                json.dumps({"now": NOW.isoformat(), "cases": cases}, ensure_ascii=False)
            )
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
