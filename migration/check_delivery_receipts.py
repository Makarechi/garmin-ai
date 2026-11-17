"""Record synthetic outbox receipts and recovery from the pinned Python implementation."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import DeliveryReceipt
from garmin_ai.dialogue import (
    explicitly_requeue_uncertain,
    record_delivery_receipt,
    recover_expired_outbox_leases,
)
from garmin_ai.events import Conflict
from garmin_ai.models import Conversation, MessageDeliveryReceipt, OutboxMessage, Person

OWNER_CONTEXT = UUID(int=101)
BASE = datetime.fromisoformat("2026-11-05T10:00:00+00:00")


def uuid(value):
    return str(UUID(int=value))


def receipts():
    return [
        ("replaced_lease", 1, 1, "provider_accepted", 0, 99, "obsolete"),
        ("provider_accepted", 1, 2, "provider_accepted", 0, 10, "opaque/accepted"),
        ("duplicate_evidence", 1, 3, "provider_accepted", 0, None, "opaque/accepted"),
        ("delivered", 1, 4, "delivered", 3, None, "opaque/delivered"),
        ("out_of_order_failure", 1, 5, "failed", 1, None, "opaque/old"),
        ("late_uncertainty", 1, 6, "uncertain", 4, None, None),
        ("read_at_same_time", 1, 7, "read", 4, None, "opaque/read"),
        ("read_survives_expiration", 1, 8, "expired", 5, None, None),
        ("queued_cancellation", 2, 9, "cancelled", 1, None, None),
    ]


def snapshot(session):
    outbox = [
        {
            "id": str(row.id),
            "state": row.state,
            "attempts": row.attempts,
            "reference": row.provider_reference,
            "lease_token": str(row.lease_token) if row.lease_token else None,
            "lease_until": row.lease_until.isoformat() if row.lease_until else None,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        }
        for row in session.scalars(select(OutboxMessage).order_by(OutboxMessage.id))
    ]
    evidence = [
        [
            str(row.id),
            str(row.outbox_message_id),
            row.state,
            row.observed_at.isoformat(),
            row.provider_reference,
            row.detail,
        ]
        for row in session.scalars(
            select(MessageDeliveryReceipt).order_by(MessageDeliveryReceipt.id)
        )
    ]
    return {"outbox": outbox, "evidence": evidence}


def seed(session):
    owner = session.scalar(select(Person.id))
    session.add(
        Conversation(
            id=OWNER_CONTEXT, owner_id=owner, channel="fixture", channel_instance_id="primary"
        )
    )
    session.flush()
    for number, state, lease_until in [
        (1, "sending", BASE + timedelta(minutes=10)),
        (2, "queued", None),
        (3, "sending", BASE - timedelta(seconds=1)),
        (4, "sending", BASE),
    ]:
        session.add(
            OutboxMessage(
                id=UUID(int=number),
                owner_id=owner,
                conversation_id=OWNER_CONTEXT,
                operation_id=UUID(int=number + 20),
                dedup_key=f"synthetic-receipt-{number}",
                intent={},
                state=state,
                attempts=1 if lease_until else 0,
                lease_until=lease_until,
                lease_token=UUID(int=number * 10) if lease_until else None,
                next_attempt_at=BASE if number == 3 else None,
            )
        )
    session.flush()


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            seed(session)
            initial = snapshot(session)
        cases = []
        for name, outbox_id, evidence_id, state, minute, lease, reference in receipts():
            observed = BASE + timedelta(minutes=minute)
            receipt = DeliveryReceipt(
                receipt_id=UUID(int=evidence_id),
                intent_id=UUID(int=outbox_id),
                state=state,
                observed_at=observed,
                provider_reference=reference,
                detail="synthetic evidence",
            )
            with Session(engine) as session, session.begin():
                try:
                    evidence = record_delivery_receipt(
                        session,
                        UUID(int=outbox_id),
                        receipt,
                        lease_token=UUID(int=lease) if lease else None,
                    )
                    outcome = str(evidence.id)
                except Conflict:
                    outcome = "LeaseReplaced"
                cases.append(
                    {
                        "name": name,
                        "target": uuid(outbox_id),
                        "lease": uuid(lease) if lease else None,
                        "receipt": receipt.model_dump(mode="json"),
                        "outcome": outcome,
                        "snapshot": snapshot(session),
                    }
                )
        with Session(engine) as session, session.begin():
            recovered = recover_expired_outbox_leases(session, BASE)
            recovery = {
                "clock": BASE.isoformat(),
                "count": recovered,
                "snapshot": snapshot(session),
            }
            cases.append({"name": "recovery", "recovery": recovery})
        with Session(engine) as session, session.begin():
            for name, message, authorized in [
                ("requires_authorization", 3, False),
                ("rejects_other_state", 1, True),
                ("reconcile", 3, True),
                ("rejects_second_requeue", 3, True),
            ]:
                try:
                    row = explicitly_requeue_uncertain(
                        session, UUID(int=message), authorized=authorized
                    )
                    outcome = row.state
                except (Conflict, PermissionError) as error:
                    outcome = type(error).__name__
                cases.append(
                    {
                        "name": name,
                        "requeue": {"id": uuid(message), "authorized": authorized},
                        "outcome": outcome,
                        "snapshot": snapshot(session),
                    }
                )
        output.write_text(json.dumps({"initial": initial, "cases": cases}), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
