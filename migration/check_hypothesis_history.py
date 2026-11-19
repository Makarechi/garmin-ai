"""Compare durable hypothesis lifecycle operations with frozen Python."""

import argparse
import copy
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_hypothesis_specs import BASE
from capture_reference import isolated_engine
from check_coffee_sleep import seed_coffee
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai import hypotheses
from garmin_ai.models import AppState, Person, SourcePayload

NOW = datetime(2026, 9, 10, tzinfo=UTC)
CHECK = datetime(2026, 10, 11, tzinfo=UTC)
IDENTITY = UUID(BASE["id"])
PENDING = UUID(int=7300001)


def request(name, action, before=None, **changes):
    return {
        "name": name,
        "action": action,
        "before": copy.deepcopy(before),
        "spec": BASE,
        "now": (CHECK if action == "recheck" else NOW).isoformat(),
        "configured": "UTC",
        **changes,
    }


def cases(registered, checked):
    yield request("register", "register")
    yield request("fetch-missing", "fetch")
    yield request("stop-missing", "stop")
    yield request("recheck-missing", "recheck")
    yield request("fetch", "fetch", registered)
    yield request("stop", "stop", registered)
    stopped = {**registered, "status": "stopped", "stopped_at": NOW.isoformat()}
    yield request("stop-idempotent", "stop", stopped, now=CHECK.isoformat())
    yield request("register-idempotent", "register", registered, now=CHECK.isoformat())
    legacy = copy.deepcopy(registered)
    legacy["spec"]["late_hours"] = 6
    yield request("register-legacy-integer", "register", legacy)
    legacy = copy.deepcopy(registered)
    legacy["spec"]["late_hours"] = True
    yield request("register-legacy-boolean", "register", legacy, spec={**BASE, "late_hours": 1})
    yield request("register-stopped-idempotent", "register", stopped)
    yield request("immutable", "register", registered, spec={**BASE, "direction": "higher"})
    for offset in [-2, -1, 0, 1, 2, 3, 4]:
        yield request(
            "registration-day-" + str(offset),
            "register",
            now=(NOW + timedelta(days=offset)).isoformat(),
        )
    for configured, timezone in [
        ("Asia/Tokyo", "UTC"),
        ("UTC", "Asia/Tokyo"),
        ("Pacific/Kiritimati", "Pacific/Kiritimati"),
        ("posix/UTC", "posix/UTC"),
        ("right/UTC", "right/UTC"),
    ]:
        yield request(
            "timezone-" + configured + "-" + timezone,
            "register",
            configured=configured,
            spec={**BASE, "timezone": timezone},
        )
    for field, values in {
        "normalization_parser_version": [None, -1, 9.0, "9"],
        "projection_generation": [None, "synthetic-generation"],
        "method_version": [None, "", False, 0, [], {}, 9, True, "synthetic-analyzer"],
    }.items():
        for index, value in enumerate(values):
            before = {**registered, field: value}
            yield request(f"changed-{field}-{index}", "recheck", before)
        before = copy.deepcopy(registered)
        del before[field]
        yield request("missing-" + field, "recheck", before)
    for offset in [-1, 0, 8, 9, 10]:
        yield request(
            "recheck-day-" + str(offset),
            "recheck",
            registered,
            now=(CHECK + timedelta(days=offset)).isoformat(),
        )
    yield request("recheck-stopped", "recheck", stopped)
    yield request("recheck-timezone-change", "recheck", registered, configured="Asia/Tokyo")
    yield request("first-check", "recheck", registered)
    yield request("same-check", "recheck", checked, now=(CHECK + timedelta(days=1)).isoformat())
    yield request("new-evidence", "recheck", checked, health_score=99.0)
    for length in [2, 10, 11]:
        for existing in [False, True]:
            before = copy.deepcopy(checked)
            before["checks"] = [
                {
                    **copy.deepcopy(checked["checks"][0]),
                    "evidence": {
                        **checked["checks"][0]["evidence"],
                        "evidence_hash": "synthetic-other-" + str(index),
                    },
                }
                for index in range(length)
            ]
            if existing:
                before["checks"][length - 1] = copy.deepcopy(checked["checks"][0])
            before.update(check_count=length, current_check_index=0)
            yield request(f"history-{length}-{existing}", "recheck", before)
    for action in ["register", "recheck", "stop", "fetch"]:
        yield request(
            "replay-" + action, action, None if action == "register" else registered, replay=True
        )
    yield request("replay-idempotent", "register", registered, replay=True)
    yield request(
        "replay-before-analysis-conflict",
        "recheck",
        {**registered, "normalization_parser_version": -1},
        replay=True,
    )
    for count in [99, 100]:
        yield request("registration-bound-" + str(count), "register", fillers=count)
    yield request("idempotent-bound", "register", registered, fillers=100)
    yield request(
        "matching-generation",
        "recheck",
        {**registered, "projection_generation": "synthetic-current"},
        generation="synthetic-current",
        has_generation=True,
    )


def setup(session, case):
    if case["before"] is not None:
        session.add(AppState(key="hypothesis:" + str(IDENTITY), value=case["before"]))
    for index in range(case.get("fillers", 0)):
        session.add(AppState(key="hypothesis:synthetic-filler:" + str(index), value={}))
    if case.get("has_generation"):
        session.add(AppState(key="replay:generation", value=case["generation"]))
    if case.get("health_score") is not None:
        session.execute(
            text("UPDATE health_days SET sleep_score=:score WHERE day='2026-10-01'"),
            {"score": case["health_score"]},
        )
    if case.get("replay"):
        session.add(
            SourcePayload(
                id=PENDING,
                source="garmin_connect",
                endpoint="daily",
                source_key="2026-09-10",
                payload_hash="c" * 64,
                archive_key="sha256:" + "c" * 64,
                fetched_at=NOW,
                parser_version=0,
                status="pending",
                payload={"totalSteps": 0},
            )
        )
    session.flush()


def invoke(session, case):
    now = datetime.fromisoformat(case["now"])
    session.info["timezone"] = case["configured"]
    if case["action"] == "register":
        return hypotheses.register(
            session, hypotheses.HypothesisSpec.model_validate(case["spec"]), now
        )
    if case["action"] == "recheck":
        return hypotheses.recheck(session, IDENTITY, now)
    if case["action"] == "stop":
        return hypotheses.stop(session, IDENTITY, now)
    return hypotheses.fetch(session, IDENTITY).value


def capture(engine):
    with Session(engine) as session:
        session.info["timezone"] = "UTC"
        registered = copy.deepcopy(
            hypotheses.register(session, hypotheses.HypothesisSpec.model_validate(BASE), NOW)
        )
        checked = copy.deepcopy(hypotheses.recheck(session, IDENTITY, CHECK))
        session.rollback()
    result = []
    for case in cases(registered, checked):
        with Session(engine) as session:
            setup(session, case)
            try:
                value = copy.deepcopy(invoke(session, case))
                session.flush()
                stored = session.get(
                    AppState, "hypothesis:" + str(IDENTITY), populate_existing=True
                )
                result.append(
                    {
                        **case,
                        "expected": value,
                        "stored": copy.deepcopy(stored.value) if stored else None,
                    }
                )
            except Exception as error:
                result.append({**case, "error": type(error).__name__, "message": str(error)})
            finally:
                session.rollback()
    return result


def run(binary):
    engine = isolated_engine()
    try:
        seed_coffee(engine)
        values = capture(engine)
        with engine.connect() as connection:
            owner = connection.execute(select(Person.id)).scalar_one()
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "hypotheses.json"
            fixture.write_text(
                json.dumps(
                    {"owner": str(owner), "cases": values}, ensure_ascii=False, allow_nan=False
                ),
                encoding="utf-8",
            )
            retained = os.environ.get("GA_TEST_HYPOTHESES_RETAIN")
            if retained:
                Path(retained).write_bytes(fixture.read_bytes())
            subprocess.run(
                [binary, "--ignored", "--exact", "compares_hypothesis_history"],
                check=True,
                env={**os.environ, "GA_TEST_HYPOTHESES": str(fixture)},
            )
        print("Matched durable hypothesis lifecycle cases", len(values), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    run(parser.parse_args().binary)
