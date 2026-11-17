"""Compare actual tracker consent changes and derived-state cleanup with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import AppState, Conversation, EventDefinition, OutboxMessage, Person
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share, revoke_tracker_share

DEFINITION = UUID(int=44)
SYSTEM = UUID(int=45)
INITIAL_EPOCH = str(UUID(int=46))
AT = datetime(2026, 3, 12, 12, tzinfo=UTC)
MEMORY_KEY = "analysis:conversation"
MODEL = "model:gemini:primary"
PRIMARY = "telegram:primary"
SECONDARY = "telegram:secondary"
OTHER = "telegram:other"


def seed(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        session.add_all(
            [
                EventDefinition(
                    id=DEFINITION,
                    owner_id=owner,
                    namespace="user",
                    key="user.synthetic_consent",
                    status="retired",
                    current_version=None,
                ),
                EventDefinition(
                    id=SYSTEM,
                    owner_id=None,
                    namespace="system",
                    key="system.synthetic_consent",
                    status="draft",
                    current_version=None,
                ),
            ]
        )
        conversations = {}
        for index, instance in enumerate((PRIMARY, SECONDARY), 100):
            conversation = Conversation(
                id=UUID(int=index),
                owner_id=owner,
                channel="telegram",
                channel_instance_id=instance,
                external_conversation_id="synthetic",
                memory_epoch=UUID(INITIAL_EPOCH),
                state={"analysis_turns": [{"source": instance}], "retained": instance},
            )
            conversations[instance] = conversation.id
            session.add(conversation)
        session.flush()
        for key, instance, evidence, state in [
            ("primary-match", PRIMARY, DEFINITION, "queued"),
            ("secondary-match", SECONDARY, DEFINITION, "queued"),
            ("secondary-unrelated", SECONDARY, SYSTEM, "queued"),
            ("secondary-delivered", SECONDARY, DEFINITION, "delivered"),
        ]:
            session.add(
                OutboxMessage(
                    owner_id=owner,
                    conversation_id=conversations[instance],
                    operation_id=UUID(int=99),
                    intent={
                        "channel_instance": {
                            "channel": "telegram",
                            "instance_id": instance.split(":")[1],
                        },
                        "evidence_refs": [f"definition:{evidence}"],
                    },
                    dedup_key=key,
                    state=state,
                    next_attempt_at=AT if state == "queued" else None,
                )
            )
        session.add(
            AppState(
                key=MEMORY_KEY,
                value={
                    "epoch": INITIAL_EPOCH,
                    "epochs": {SECONDARY: INITIAL_EPOCH, OTHER: INITIAL_EPOCH},
                    "turns": [
                        {"update_id": "primary-turn"},
                        {"update_id": "secondary-turn", "channel_instance_id": SECONDARY},
                        {"update_id": "other-turn", "channel_instance_id": OTHER},
                    ],
                    "extra": "synthetic retained value",
                },
            )
        )
        for instance in (PRIMARY, SECONDARY, OTHER):
            suffix = "" if instance == PRIMARY else f":{instance}"
            session.add(
                AppState(
                    key=f"{MEMORY_KEY}:pending{suffix}",
                    value={
                        "epoch": INITIAL_EPOCH,
                        "turn": {
                            "update_id": instance + "-pending",
                            "channel_instance_id": instance,
                        },
                    },
                )
            )
        for key, kind, site in [
            ("primary-turn", "note", None),
            ("secondary-turn", "note", None),
            ("other-turn", "note", OTHER),
            (PRIMARY + "-pending", "note", None),
            (SECONDARY + "-pending", "note", None),
            (OTHER + "-pending", "note", OTHER),
            ("primary-cached", "analysis", None),
            ("secondary-cached", "analysis", SECONDARY),
            ("other-cached", "analysis", OTHER),
            ("ordinary", "note", SECONDARY),
        ]:
            value = {
                "text": "synthetic cached text",
                "kind": kind,
                "status": "recorded",
                "keyboard": True,
            }
            if site is not None:
                value["channel_instance_id"] = site
            session.add(AppState(key=f"telegram:reply:{key}", value=value))


def snapshot(session):
    def normalize(value):
        if value == INITIAL_EPOCH:
            return "initial"
        try:
            UUID(value)
        except (TypeError, ValueError, AttributeError):
            return value
        return "rotated"

    states = {}
    for row in session.scalars(
        select(AppState).where(
            AppState.key.startswith("tracker-consent:")
            | AppState.key.startswith(MEMORY_KEY)
            | AppState.key.startswith("telegram:reply:")
        )
    ):
        value = dict(row.value)
        if "categories" in value:
            value["categories"] = sorted(value["categories"])
        if "epoch" in value:
            value["epoch"] = normalize(value["epoch"])
        if "epochs" in value:
            value["epochs"] = {key: normalize(epoch) for key, epoch in value["epochs"].items()}
        states[row.key] = value
    return {
        "states": states,
        "conversations": {
            str(row.id): [row.state, normalize(str(row.memory_epoch))]
            for row in session.scalars(select(Conversation))
        },
        "outbox": {
            row.dedup_key: [row.state, row.next_attempt_at is not None]
            for row in session.scalars(select(OutboxMessage))
        },
    }


def fixtures(engine):
    seed(engine)
    all_categories = ["schema", "facts", "original_text"]
    steps = [
        ("forbidden", "grant", MODEL, all_categories, DEFINITION, AT, False),
        ("grant_model", "grant", MODEL, all_categories, DEFINITION, AT, True),
        ("grant_primary", "grant", PRIMARY, all_categories, DEFINITION, AT, True),
        ("grant_secondary", "grant", SECONDARY, all_categories, DEFINITION, AT, True),
        (
            "channel_original_text_removed",
            "grant",
            SECONDARY,
            ["schema", "facts"],
            DEFINITION,
            AT,
            True,
        ),
        ("channel_facts_removed", "grant", SECONDARY, ["schema"], DEFINITION, AT, True),
        ("missing_consent", "revoke", "telegram:missing", [], DEFINITION, AT, True),
        ("model_schema_removed", "grant", MODEL, ["facts", "original_text"], DEFINITION, AT, True),
        ("model_original_text_removed", "grant", MODEL, ["facts"], DEFINITION, AT, True),
        ("revoke_model", "revoke", MODEL, [], DEFINITION, AT, True),
        ("revoke_primary", "revoke", PRIMARY, [], DEFINITION, AT, True),
        (
            "future_grant",
            "grant",
            PRIMARY,
            ["schema"],
            DEFINITION,
            datetime(9999, 1, 1, tzinfo=UTC),
            True,
        ),
        ("empty_categories", "grant", PRIMARY, [], DEFINITION, AT, True),
        ("system_definition", "grant", PRIMARY, ["schema"], SYSTEM, AT, True),
    ]
    results = []
    for name, operation, instance, categories, definition, at, authorized in steps:
        kind = "model" if instance.startswith("model:") else "channel"
        try:
            with Session(engine) as session, session.begin():
                if operation == "grant":
                    consent = TrackerShareConsent(
                        definition_id=definition,
                        destination_kind=kind,
                        destination_instance_id=instance,
                        categories=set(categories),
                        granted_at=at,
                    )
                    grant_tracker_share(session, consent, authorized=authorized)
                    outcome = "granted"
                else:
                    deleted = revoke_tracker_share(
                        session, definition, kind, instance, authorized=authorized
                    )
                    outcome = "revoked" if deleted else "absent"
        except (PermissionError, ValueError, LookupError) as exc:
            outcome = "invalid" if isinstance(exc, ValueError) else type(exc).__name__
        with Session(engine) as session:
            state = snapshot(session)
        results.append(
            {
                "name": name,
                "operation": operation,
                "kind": kind,
                "instance": instance,
                "categories": categories,
                "definition_id": str(definition),
                "granted_at": at.isoformat(),
                "authorized": authorized,
                "outcome": outcome,
                "state": state,
            }
        )
    assert results[5]["state"]["outbox"]["secondary-match"] == ["cancelled", False]
    assert results[5]["state"]["outbox"]["primary-match"] == ["queued", True]
    assert "analysis_turns" not in results[8]["state"]["conversations"][str(UUID(int=100))][0]
    return results


def run(binary):
    primary_url = os.environ["GA_TEST_DATABASE_URL"]
    native_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    assert primary_url != native_url
    python = isolated_engine()
    try:
        os.environ["GA_TEST_DATABASE_URL"] = native_url
        native = isolated_engine()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = primary_url
    try:
        tests = fixtures(python)
        seed(native)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "consent-writes.json"
            path.write_text(json.dumps(tests), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={
                    **os.environ,
                    "GA_TEST_DATABASE_URL": native_url,
                    "GA_TEST_CONSENT_WRITE_CASES": str(path),
                },
            )
        print(f"Matched {len(tests)} tracker consent write outcomes against pinned Python")
    finally:
        python.dispose()
        native.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
