"""Compare model diary visibility with pinned Python and verify hidden pagination."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.models import (
    AppState,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    ModuleConfig,
    Person,
)
from garmin_ai.queries import list_events
from garmin_ai.share_policy import TrackerShareConsent

START = datetime(2026, 10, 25, 9, tzinfo=UTC)
END = START + timedelta(hours=1)
TRACKER = UUID(int=41)
FUTURE = UUID(int=42)
PRIVATE = UUID(int=43)
SENSITIVE = UUID(int=44)
HIDDEN = UUID(int=45)


def seed(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        session.add_all(
            [
                EventDefinition(
                    id=identity,
                    owner_id=owner,
                    namespace="user",
                    key=key,
                    status="active",
                    current_version=current,
                )
                for identity, key, current in [
                    (TRACKER, "user.tracker", 2),
                    (FUTURE, "user.future", 1),
                ]
            ]
        )
        session.flush()
        session.add_all(
            [
                EventDefinitionVersion(
                    id=identity,
                    definition_id=definition,
                    version=number,
                    schema={},
                    schema_hash="0" * 64,
                    topology="point",
                    field_metadata={},
                    labels={"en": "Synthetic tracker"},
                    privacy=privacy,
                    allowed_operations=["query", "update"],
                )
                for identity, definition, number, privacy in [
                    (PRIVATE, TRACKER, 1, "private"),
                    (SENSITIVE, TRACKER, 2, "sensitive"),
                    (HIDDEN, FUTURE, 1, "sensitive"),
                ]
            ]
        )
        session.flush()
        for number, kind, version, delta in [
            (1, "user.tracker", SENSITIVE, 10),
            (2, "migraine", None, 15),
            (3, "user.tracker", PRIVATE, 20),
            (4, "note", None, 30),
        ]:
            session.add(
                Event(
                    id=UUID(int=number),
                    kind=kind,
                    start=START + timedelta(seconds=delta),
                    timezone="UTC",
                    definition_version_id=version,
                    source="manual",
                    original_text="synthetic quote",
                    payload={"synthetic": number},
                )
            )
        session.execute(
            text("""
            INSERT INTO events(id,kind,start,timezone,source,confidence,status,original_text,
                payload,topology,envelope_version,time_precision,assertion_kind,producer,
                evidence_refs,validation_status,revision,deleted,definition_version_id)
            SELECT gen_random_uuid(),'user.future',:start,'UTC','manual',1,'confirmed','synthetic quote',
                jsonb_build_object('synthetic_hidden',n),'point',1,'instant','user_report','owner',
                '[]','schema_validated',1,false,:version
            FROM generate_series(1,1200) AS n
            """),
            {"start": START, "version": HIDDEN},
        )
        session.add(
            ModuleConfig(
                owner_id=owner,
                pack_key="general_diary",
                tracking_enabled=True,
                collection_enabled=True,
                reminders_enabled=False,
                visible=True,
                llm_enabled=True,
            )
        )
        session.add(AppState(key="preferences:onboarding", value={"model_categories": ["diary"]}))
        now = datetime.now(UTC)
        for definition, kind, instance, categories, granted in [
            (TRACKER, "model", "model:primary", {"facts"}, now),
            (TRACKER, "channel", "telegram:allowed", {"schema", "facts"}, now),
            (TRACKER, "channel", "telegram:schema", {"schema"}, now),
            (FUTURE, "model", "model:future", {"facts"}, now + timedelta(days=1)),
        ]:
            consent = TrackerShareConsent(
                definition_id=definition,
                destination_kind=kind,
                destination_instance_id=instance,
                categories=categories,
                granted_at=granted,
            )
            session.add(
                AppState(
                    key=f"tracker-consent:{definition}:{kind}:{instance}",
                    value=consent.model_dump(mode="json"),
                )
            )


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        cases = []
        for model, channel, kind, limit in [
            ("model:primary", None, None, 10),
            ("model:primary", None, None, 1),
            ("model:primary", "telegram:allowed", None, 10),
            ("model:primary", "telegram:schema", "user.tracker", 10),
            ("model:primary", "telegram:other", None, 1),
            ("model:other", "telegram:allowed", None, 10),
            ("model:other", None, "migraine", 10),
            ("model:other", None, "note", 10),
            ("model:future", None, None, 1),
            ("model:future", None, "user.future", 10),
            ("model:future", None, "user.missing", 10),
        ]:
            # A future consent has the same access as no consent. The reference's
            # post-filter can count those denied rows before excluding them.
            reference_model = "model:without-grant" if model == "model:future" else model
            info = {"llm_access": True, "model_provider_instance_id": reference_model}
            if channel:
                info["channel_destination_instance_id"] = channel
            with Session(engine, info=info) as session:
                try:
                    expected = list_events(session, START, END, kind, limit)
                except ValueError as exc:
                    assert kind == "user.missing" and str(exc) == "Unknown event kind"
                    expected = {"rows": [], "truncated": False}
                cases.append(
                    {
                        "start": START.isoformat(),
                        "end": END.isoformat(),
                        "model": model,
                        "channel": channel,
                        "kind": kind,
                        "limit": limit,
                        "expected": expected,
                        "tracker_versions": sorted(
                            {
                                row["definition_version_id"]
                                for row in expected["rows"]
                                if row["kind"].startswith("user.")
                            }
                        ),
                        "channel_versions": sorted(
                            session.info.get("channel_share_requirements", {})
                        ),
                    }
                )
        assert cases[8]["expected"]["truncated"]
        assert len(cases[8]["expected"]["rows"]) == 1
        assert cases[9]["expected"] == cases[10]["expected"]
        assert cases[0]["tracker_versions"] == [str(PRIVATE), str(SENSITIVE)]
        assert cases[0]["channel_versions"] == []
        assert cases[1]["tracker_versions"] == [str(SENSITIVE)]
        assert cases[3]["tracker_versions"] == [str(PRIVATE)]
        assert cases[2]["channel_versions"] == [str(PRIVATE), str(SENSITIVE)]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "model-diary.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_MODEL_DIARY": str(path)},
            )
        print("Matched eleven scoped diary pages and verified pagination through 1,200 hidden rows")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
