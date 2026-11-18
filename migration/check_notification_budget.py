"""Record Python's shared reminder usage against a fresh synthetic installation."""

import argparse
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.initiative_rules import RuleDefinition, TrackerRuleInstance, save_rule
from garmin_ai.models import (
    AppState,
    Conversation,
    MessageDeliveryReceipt,
    OutboxMessage,
    PendingQuestion,
    Person,
)
from garmin_ai.proactive import notification_count

ZONE = "Europe/Bratislava"


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def seed(session):
    person = session.scalar(select(Person))
    if person is None:
        person = Person()
        session.add(person)
        session.flush()
    conversation = Conversation(
        owner_id=person.id,
        channel="telegram",
        channel_instance_id="fixture",
        external_conversation_id="42",
    )
    session.add(conversation)
    session.flush()
    rule = save_rule(
        session,
        TrackerRuleInstance(
            conversation_id=conversation.id,
            definition_version_id=uuid4(),
            topic="synthetic",
            timezone=ZONE,
            rule=RuleDefinition(
                kind="schedule", prompt="Synthetic prompt", local_time=time(22, 30)
            ),
            primary_channel=ChannelInstanceRef(channel="telegram", instance_id="fixture"),
        ),
    )
    for index, sent in enumerate([utc(24, 21, 59), utc(24, 22), utc(25, 10), None]):
        session.add(
            PendingQuestion(
                kind="context",
                text="Synthetic question",
                evidence={},
                priority=0.8,
                status="answered" if sent else "pending",
                earliest_send_at=utc(24, 12),
                expires_at=utc(27, 12),
                sent_at=sent,
                dedup_key=f"fixture-question:{index}",
            )
        )
    insight_key = "insight:last:fixture"
    session.add(AppState(key=insight_key, value={"at": utc(24, 23).isoformat()}))
    session.add(AppState(key="insight:last:old", value={"at": utc(24, 21).isoformat()}))
    session.add(AppState(key="insight:last:future", value={"at": utc(25, 12).isoformat()}))

    def add(key, created, state="queued", *, initiative=True, scheduled=False, **fields):
        marker = f"rule:{rule.id}"
        intent = {"initiative": initiative, "evidence_refs": [marker] if scheduled else []}
        outbox = OutboxMessage(
            owner_id=person.id,
            conversation_id=conversation.id,
            operation_id=uuid4(),
            intent=intent,
            dedup_key=key,
            state=state,
            created_at=created,
            updated_at=fields.pop("updated_at", created),
            **fields,
        )
        session.add(outbox)
        session.flush()
        return outbox.id

    def receipt(outbox, state, when):
        session.add(MessageDeliveryReceipt(outbox_message_id=outbox, state=state, observed_at=when))

    today = add("new-today", utc(25, 5))
    add("legacy-dedup:2026-10-25", utc(23, 14))
    add("after-next-midnight", utc(25, 6), next_attempt_at=utc(25, 23))
    add("today-retry", utc(24, 13), next_attempt_at=utc(25, 6))
    add("other-reply", utc(25, 5), initiative=False)
    add("late-in-long-dst-day", utc(25, 22, 30))
    add("legacy-fallback:2026-10-24:fallback:1", utc(24, 21), scheduled=True)
    contested = add(
        "legacy-uncertain:2026-10-24",
        utc(24, 21),
        "uncertain",
        attempts=1,
        scheduled=True,
        updated_at=utc(25, 0),
    )
    receipt(contested, "uncertain", utc(24, 21, 30))
    latest = add("first-receipt-today", utc(24, 12), "cancelled")
    receipt(latest, "provider_accepted", utc(25, 1))
    receipt(latest, "delivered", utc(25, 2))
    reconfirmed = add("reconfirmed", utc(24, 21), "failed", attempts=2)
    receipt(reconfirmed, "uncertain", utc(24, 21, 35))
    receipt(reconfirmed, "provider_accepted", utc(25, 2))
    recollected = add("became-uncertain-again", utc(24, 21), "failed", attempts=2)
    receipt(recollected, "uncertain", utc(24, 21, 40))
    receipt(recollected, "provider_accepted", utc(25, 1))
    receipt(recollected, "uncertain", utc(25, 2))
    session.flush()
    return today, contested, insight_key


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            today, contested, insight = seed(session)
            cases = []

            def record(name, now, zone=ZONE, outbox=None, insight_key=None):
                count = notification_count(
                    session,
                    SimpleNamespace(timezone=zone),
                    now,
                    exclude_insight_key=insight_key,
                    exclude_outbox_id=outbox,
                )
                cases.append(
                    dict(
                        name=name,
                        now=now.isoformat(),
                        timezone=zone,
                        outbox=None if outbox is None else str(outbox),
                        insight_key=insight_key,
                        count=count,
                    )
                )
                return count

            morning = utc(25, 8)
            normal = record("DST day with legacy midnight carry", morning)
            assert (
                record("same-day retry excludes its own slot", morning, outbox=today) == normal - 1
            )
            assert (
                record("uncertain midnight retry excludes its own slot", morning, outbox=contested)
                == normal - 1
            )
            assert (
                record("insight retry excludes its own slot", morning, insight_key=insight)
                == normal - 1
            )
            carry_end = utc(25, 9, 30)
            assert (
                record("just before wall-clock carry ends", carry_end - timedelta(microseconds=1))
                == normal
            )
            assert record("wall-clock carry expired during DST transition", carry_end) == normal - 1
            assert (
                record(
                    "still counts uncertain attempt after carry expires",
                    carry_end,
                    outbox=contested,
                )
                == normal - 2
            )
            record("same instant with UTC day boundary", morning, "UTC")
            record("last microsecond of 25-hour local day", utc(25, 23) - timedelta(microseconds=1))
            record("new local day", utc(25, 23))
            record("already spent before the local midnight", utc(24, 21, 45))
        output.write_text(json.dumps(cases), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
