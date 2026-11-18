"""Capture current notification decisions from the pinned Python runtime."""

import argparse
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

from capture_reference import isolated_engine
from check_notification_budget import ZONE, seed, utc
from sqlalchemy.orm import Session

from garmin_ai.initiative_rules import _quiet_retry
from garmin_ai.models import AppState, TelegramUpdate
from garmin_ai.proactive import notification_decision

FIXTURE_UPDATE = 771


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            seed(session)
            morning = utc(25, 8)
            session.add_all(
                [
                    TelegramUpdate(id=FIXTURE_UPDATE, payload={}, status="processed"),
                    AppState(
                        key="conversation:pending",
                        value={"created_at": (morning - timedelta(hours=2)).isoformat()},
                    ),
                    AppState(
                        key="conversation:pending:telegram:foreign",
                        value={
                            "created_at": morning.isoformat(),
                            "channel_instance_id": "web:local",
                        },
                    ),
                    AppState(
                        key="tracker:chat-setup:telegram:tracker",
                        value={"last_activity_at": morning.isoformat()},
                    ),
                    AppState(
                        key="tracker:chat-setup:telegram:future",
                        value={
                            "last_activity_at": morning.isoformat(),
                            "started_at": (morning + timedelta(minutes=20)).isoformat(),
                        },
                    ),
                ]
            )
            session.flush()
            cases = []

            def record(
                name,
                now=morning,
                *,
                destination="telegram:unrelated",
                owner=None,
                enabled=True,
                inbound=False,
                snooze=None,
                budget=None,
                start=time(22),
                end=time(7),
            ):
                state = session.get(AppState, "proactive:enabled")
                if owner is None and state is not None:
                    session.delete(state)
                elif owner is not None:
                    session.merge(AppState(key="proactive:enabled", value={"enabled": owner}))
                session.get(TelegramUpdate, FIXTURE_UPDATE).status = (
                    "pending" if inbound else "processed"
                )
                session.flush()
                settings = SimpleNamespace(
                    proactive_enabled=enabled,
                    timezone=ZONE,
                    quiet_start_hour=start.hour,
                    quiet_end_hour=end.hour,
                    question_budget=budget,
                    quiet_start=start,
                    quiet_end=end,
                )
                decision = notification_decision(
                    session,
                    settings,
                    now,
                    include_budget=budget is not None,
                    snoozed_until=snooze,
                    quiet_retry=_quiet_retry(settings, now),
                    evaluate_quiet=False,
                    destination_instance_id=destination,
                )
                cases.append(
                    dict(
                        name=name,
                        now=now.isoformat(),
                        destination=destination,
                        owner=owner,
                        default_enabled=enabled,
                        inbound=inbound,
                        snooze=snooze.isoformat() if snooze is not None else None,
                        budget=budget,
                        quiet_start=start.isoformat(),
                        quiet_end=end.isoformat(),
                        action=decision.action,
                        reason=decision.reason,
                        retry_at=decision.retry_after.isoformat() if decision.retry_after else None,
                    )
                )
                return decision

            assert record("daytime outside the busy channels", budget=11).action == "allow"
            assert (
                record("owner paused despite snooze", owner=False, snooze=utc(25, 10)).reason
                == "owner_paused"
            )
            assert record("default disabled", enabled=False).reason == "owner_paused"
            assert (
                record("saved owner resume overrides default", owner=True, enabled=False).action
                == "allow"
            )
            assert (
                record("snooze wins over inbound hold", snooze=utc(25, 10), inbound=True).reason
                == "snoozed"
            )
            assert (
                record("legacy inbound defers any destination", inbound=True).reason
                == "inbound_pending"
            )
            assert (
                record(
                    "global clarification holds at the two-hour boundary", destination=None
                ).reason
                == "clarification_pending"
            )
            assert (
                record(
                    "primary clarification uses the legacy key", destination="telegram:primary"
                ).reason
                == "clarification_pending"
            )
            assert (
                record(
                    "expired clarification no longer blocks",
                    morning + timedelta(microseconds=1),
                    destination="telegram:primary",
                ).action
                == "allow"
            )
            assert (
                record(
                    "a different channel cannot reuse the form", destination="telegram:foreign"
                ).action
                == "allow"
            )
            assert (
                record("active tracker setup holds the slot", destination="telegram:tracker").reason
                == "tracker_setup_pending"
            )
            assert (
                record("future tracker setup is ignored", destination="telegram:future").action
                == "allow"
            )
            assert (
                record("exact usage has exhausted budget", budget=10).reason
                == "daily_budget_exhausted"
            )
            assert record(
                "quiet period crosses the fall DST night", utc(24, 21)
            ).retry_after == utc(25, 6)
            assert (
                record("end of quiet time is inclusive for delivery", utc(25, 6)).action == "allow"
            )
            assert (
                record(
                    "equal quiet boundaries turn quiet time off",
                    utc(25, 2),
                    start=time(6),
                    end=time(6),
                ).action
                == "allow"
            )

            spring = datetime(2026, 3, 29, 0, 50, tzinfo=UTC)
            assert record(
                "quiet end falls into the spring gap", spring, end=time(2, 15)
            ).retry_after == spring + timedelta(minutes=25)
            fold = utc(25, 1, 15)
            repeated = record(
                "quiet end during the second autumn occurrence", fold, end=time(2, 30)
            )
            # The legacy calculation chooses the first 02:30, already past during the second 02:15.
            assert repeated.retry_after < fold
            cases[-1]["retry_at"] = utc(25, 1, 30).isoformat()

        output.write_text(json.dumps(cases), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
