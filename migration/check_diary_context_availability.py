"""Record the pinned Python claim decisions for synthetic diary context dependencies."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.jobs import claim, enqueue
from garmin_ai.models import AppState, Job

NOW = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
DAY = "2026-11-18"


def capture(output):
    current = {"kind": "garmin_endpoint", "payload": {"endpoint": "heart_rate", "key": DAY}}
    failed = {
        **current,
        "status": "failed",
        "completed_at": (NOW - timedelta(minutes=5)).isoformat(),
    }
    paused = {"integration:garmin": {"status": "reauth_required"}}
    fresh_key = f"freshness:heart_rate:{DAY}"
    old = (NOW - timedelta(minutes=10)).isoformat()
    new = (NOW - timedelta(minutes=1)).isoformat()
    cases = [
        ("empty", [], {}),
        ("wait_current", [current], {}),
        ("skip_history", [{**current, "payload": {**current["payload"], "backfill": True}}], {}),
        ("paused_with_work", [current], paused),
        ("recent_failure", [failed], {}),
        ("old_success", [failed], {fresh_key: {"success_at": old, "status": "normalized"}}),
        ("error_success", [failed], {fresh_key: {"success_at": new, "status": "fetch_error"}}),
        ("explicit_null", [failed], {fresh_key: {"normalized_at": None, "success_at": new}}),
        ("successful_recovery", [failed], {fresh_key: {"normalized_at": new, "status": "error"}}),
        (
            "old_failure_ignored",
            [{**failed, "completed_at": (NOW - timedelta(hours=4)).isoformat()}],
            {},
        ),
        (
            "activity_page_dependency",
            [{"kind": "garmin_activities", "payload": {"offset": 100}}],
            {},
        ),
    ]
    results = []
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            for name, jobs, state in cases:
                for i, job in enumerate(jobs):
                    completed = job.get("completed_at")
                    session.add(
                        Job(
                            kind=job["kind"],
                            payload=job["payload"],
                            dedup_key=f"{name}:{i}",
                            status=job.get("status", "pending"),
                            run_at=NOW,
                            completed_at=datetime.fromisoformat(completed) if completed else None,
                        )
                    )
                session.add_all(AppState(key=key, value=value) for key, value in state.items())
                session.flush()
                enqueue(session, "agent_proactive", {}, f"{name}:proactive", NOW)
                claimed = claim(session, now=NOW, kinds=["agent_proactive"], backups_enabled=False)
                result = (
                    "WaitingForSync"
                    if claimed is None
                    else "Withheld"
                    if claimed.payload["context_sync_failures"] or claimed.payload["replay_pending"]
                    else "Ready"
                )
                results.append({"name": name, "jobs": jobs, "state": state, "expected": result})
                session.rollback()
        output.write_text(json.dumps({"now": NOW.isoformat(), "cases": results}), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
