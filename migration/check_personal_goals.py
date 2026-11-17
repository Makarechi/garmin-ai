"""Capture current goal preference ordering on a disposable database."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.events import Conflict
from garmin_ai.models import AppState
from garmin_ai.personal_goals import KEY, CommandOrder, GoalSelection, preferences, select_goals

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


def run(output):
    def command(offset, received, tie):
        return {
            "source_epoch": int(NOW.timestamp()) + offset,
            "received_at": (NOW + timedelta(microseconds=received)).isoformat(),
            "tie_breaker": tie,
        }

    cases = [
        (0, ["sleep", "migraine"], 100, None),
        (0, ["running"], 200, None),
        (1, ["running"], 300, command(0, 99, "900")),
        (1, ["sleep", "migraine"], 400, command(0, 101, "a1")),
        (1, ["running"], 500, command(0, 101, "a0")),
        (1, ["running"], 600, command(0, 101, "a2")),
        (2, ["running"], 700, None),
        (2, [], 800, command(0, 700, "b0")),
        (2, [], 900, command(0, 701, "b1")),
        (3, [], 1000, command(0, 702, "b2")),
        (3, ["sleep", "sleep"], 1100, None),
    ]
    cases += [
        (3 + offset, ["wellbeing"] if offset % 2 == 0 else [], 2000 + offset, None)
        for offset in range(23)
    ]
    results = []
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            assert preferences(session) == {
                "configured": False,
                "revision": 0,
                "goals": [],
                "updated_at": None,
            }
            for revision, goals, micros, order in cases:
                now = NOW + timedelta(microseconds=micros)
                try:
                    selected = select_goals(
                        session,
                        GoalSelection(revision=revision, goals=goals),
                        now,
                        command_order=CommandOrder.model_validate(order) if order else None,
                    )
                    session.commit()
                except Conflict:
                    selected = "conflict"
                    session.rollback()
                except ValueError:
                    selected = "invalid"
                    session.rollback()
                value = session.get(AppState, KEY).value.copy()
                assert value.pop("owner_id")
                results.append(
                    {
                        "revision": revision,
                        "goals": goals,
                        "at": now.isoformat(),
                        "command": order,
                        "value": value,
                        "result": selected,
                    }
                )
            output.write_text(json.dumps(results, ensure_ascii=False))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)


if __name__ == "__main__":
    main()
