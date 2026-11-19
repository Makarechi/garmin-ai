"""Compare native cutoff reconstruction with Python generic entry queries."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from garmin_ai.definitions import ensure_system_definitions
from garmin_ai.models import (
    AppState,
    Audit,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    MetricObservation,
    Person,
    SourcePayload,
)
from garmin_ai.scenario_packs import ensure_scenario_packs
from garmin_ai.share_policy import TrackerShareConsent
from garmin_ai.source_metric_projection import has_source_observations
from garmin_ai.tools import call_tool

AT = datetime(2026, 10, 25, 9, tzinfo=UTC)
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
T1, T2, T3 = [T0 + timedelta(hours=index) for index in (1, 2, 3)]
TRACKER = UUID(int=80000)
VERSION1, VERSION2 = UUID(int=80001), UUID(int=80002)


def state(
    identity,
    version,
    start=AT,
    end=None,
    revision=1,
    deleted=False,
    topology="flexible",
    description="Synthetic historical entry",
):
    return {
        "id": str(identity),
        "definition_version_id": str(version),
        "kind": "user.history",
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "revision": revision,
        "deleted": deleted,
        "topology": topology,
        "payload": {"description": description},
        "status": "confirmed",
        "validation_status": "schema_validated",
        "assertion_kind": "user_report",
        "source": "manual",
    }


def row(
    session,
    identity,
    key,
    version,
    start=AT,
    end=None,
    ingested=T0,
    updated=T0,
    revision=1,
    payload=None,
    topology="flexible",
):
    session.add(
        Event(
            id=identity,
            kind=key,
            definition_version_id=version,
            start=start,
            end=end,
            timezone="UTC",
            source="manual",
            revision=revision,
            topology=topology,
            payload=payload or {"description": "Synthetic live entry"},
            ingested_at=ingested,
            updated_at=updated,
            recorded_at=ingested,
        )
    )


def audit(session, identity, at, before, after):
    session.add(
        Audit(
            event_id=identity,
            action="update",
            before=before,
            after=after,
            actor="synthetic",
            created_at=at,
        )
    )


def seed(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        system = ensure_system_definitions(session)
        ensure_scenario_packs(session, legacy_install=True)
        session.add(
            EventDefinition(
                id=TRACKER,
                owner_id=owner,
                namespace="user",
                key="user.history",
                status="retired",
                current_version=2,
            )
        )
        session.flush()
        for number, identity, privacy in [(1, VERSION1, "private"), (2, VERSION2, "sensitive")]:
            session.add(
                EventDefinitionVersion(
                    id=identity,
                    definition_id=TRACKER,
                    version=number,
                    schema={},
                    schema_hash="0" * 64,
                    topology="flexible",
                    field_metadata={},
                    labels={"en": "Synthetic history"},
                    privacy=privacy,
                    allowed_operations=["query"],
                )
            )
        session.flush()
        identity = UUID(int=81000)
        first = state(
            identity, VERSION1, end=AT + timedelta(minutes=30), description="Synthetic original"
        )
        second = state(
            identity,
            VERSION2,
            start=AT + timedelta(hours=5),
            revision=2,
            description="Synthetic moved entry",
        )
        deleted = {**second, "revision": 3, "deleted": True}
        restored = state(
            identity,
            VERSION1,
            start=AT + timedelta(minutes=15),
            revision=4,
            description="Synthetic restored entry",
        )
        row(
            session,
            identity,
            "user.history",
            VERSION1,
            start=AT + timedelta(minutes=15),
            updated=T3,
            revision=4,
            payload=restored["payload"],
        )
        for at, before, after in [
            (T0, None, first),
            (T1, first, second),
            (T2, second, deleted),
            (T3, deleted, restored),
        ]:
            audit(session, identity, at, before, after)
        row(
            session, UUID(int=81001), "user.history", VERSION1, start=AT.replace(microsecond=123000)
        )
        row(session, UUID(int=81002), "user.history", VERSION1, ingested=T2, updated=T2)
        legacy = state(UUID(int=81003), VERSION1, start=AT, description="Synthetic legacy offset")
        legacy["start"] = "2026-10-25T10:00:00+02:00"
        legacy.pop("topology")
        row(session, UUID(int=81003), "user.history", VERSION1, updated=T2)
        audit(session, UUID(int=81003), T1, legacy, {**legacy, "revision": 2})
        row(session, UUID(int=81004), "user.history", VERSION1, updated=T2)
        before = state(
            UUID(int=81004), VERSION1, revision=2, description="Synthetic future preimage"
        )
        audit(session, UUID(int=81004), T2, before, {**before, "revision": 3})
        for index, key in [(1, "user.unavailable"), (2, "user.null_after"), (3, "user.draft")]:
            definition = UUID(int=82000 + index)
            version = UUID(int=83000 + index)
            session.add(
                EventDefinition(
                    id=definition,
                    owner_id=owner,
                    namespace="user",
                    key=key,
                    status="draft" if index == 3 else "active",
                    current_version=None if index == 3 else 1,
                )
            )
            session.flush()
            if index != 3:
                session.add(
                    EventDefinitionVersion(
                        id=version,
                        definition_id=definition,
                        version=1,
                        schema={},
                        schema_hash="0" * 64,
                        topology="flexible",
                        field_metadata={},
                        labels={},
                        privacy="private",
                        allowed_operations=["query"],
                    )
                )
                session.flush()
                identity = UUID(int=84000 + index)
                row(session, identity, key, version, updated=T3)
                if index == 2:
                    audit(session, identity, T1, None, None)
                    before = state(identity, version, description="Synthetic ignored future image")
                    audit(session, identity, T2, before, {**before, "revision": 2})
        migraine = system["migraine"]
        identity = UUID(int=85000)
        row(
            session,
            identity,
            "migraine",
            migraine.id,
            start=AT - timedelta(days=1),
            topology="open_interval",
        )
        old = state(identity, migraine.id, start=AT - timedelta(days=1), topology=None)
        old["kind"] = "migraine"
        old["payload"] = {"type": "migraine", "notes": "Synthetic legacy open episode"}
        row(
            session,
            UUID(int=85001),
            "migraine",
            migraine.id,
            start=datetime(1900, 1, 1, tzinfo=UTC),
            topology="open_interval",
            payload={"type": "migraine", "notes": "Synthetic historical timezone probe"},
        )
        audit(session, identity, T0, None, old)
        session.add(
            MetricObservation(
                metric="user.synthetic_source",
                value=1.0,
                unit="count",
                observed_at=AT,
                source_calendar_date=AT.date(),
                source_ref=UUID(int=86000),
                fetched_at=AT,
                timezone="UTC",
                quality="observed",
                valid=False,
                sequence=0,
                feature_version="source-plugin-v1",
            )
        )
        return owner


def arguments(key="user.history", cutoff=T3, **changes):
    return {
        "operation": "query_entries",
        "definition_key": key,
        "start": (AT - timedelta(hours=1)).isoformat(),
        "end": (AT + timedelta(hours=3)).isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        **changes,
    }


def cases(owner):
    result = []
    for index, cutoff in enumerate(
        [
            T0 - timedelta(microseconds=1),
            T0,
            T1 - timedelta(microseconds=1),
            T1,
            T2,
            T3,
            T3 + timedelta(days=1),
        ]
    ):
        result.append({"name": f"cutoff-{index}", "spec": arguments(cutoff=cutoff)})
    result += [
        {
            "name": "moved-window",
            "spec": arguments(
                cutoff=T1,
                start=(AT + timedelta(hours=5)).isoformat(),
                end=(AT + timedelta(hours=6)).isoformat(),
            ),
        },
        {"name": "explicit-limit", "spec": arguments(limit=1)},
        {"name": "unavailable", "spec": arguments("user.unavailable", T1)},
        {"name": "null-after", "spec": arguments("user.null_after", T1)},
        {"name": "draft", "spec": arguments("user.draft")},
        {"name": "missing", "spec": arguments("user.absent")},
        {"name": "open-start", "spec": arguments("system.migraine")},
        {"name": "open-overlap", "spec": arguments("system.migraine", time_relation="overlap")},
        {"name": "offset-cutoff", "spec": arguments(knowledge_cutoff="2026-11-01T17:00:00+02:00")},
        {
            "name": "earliest-cutoff",
            "spec": arguments(knowledge_cutoff="0001-01-01T00:00:00+01:00"),
        },
        {"name": "latest-cutoff", "spec": arguments(knowledge_cutoff="9999-12-31T23:59:59-01:00")},
    ]
    result += [
        {"name": "source-history-full", "spec": arguments(metric_key="user.synthetic_source")},
        {
            "name": "source-history-limited",
            "spec": arguments(metric_key="user.synthetic_source"),
            "read_health": False,
        },
        {"name": "diary-only-reader", "spec": arguments(), "read_health": False},
    ]
    return result + [
        {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in result
    ]


def capture(engine, cases):
    captured = []
    for case in cases:
        with Session(engine) as session, session.begin():
            if case.get("owner"):
                session.info.update(llm_access=True, model_provider_instance_id="model:mcp:local")
            try:
                if not case.get("owner") and not case.get("read_health", True):
                    session.execute(select(func.pg_advisory_xact_lock_shared(72104619)))
                    metric = case["spec"].get("metric_key")
                    if metric and has_source_observations(session, metric):
                        raise PermissionError("Insufficient scope")
                expected = call_tool(session, "generic_analysis", {"spec": case["spec"]})
                captured.append({**case, "expected": expected})
            except Exception as error:
                captured.append({**case, "error": type(error).__name__})
    return captured


def seed_large(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        for definition, version, key in [
            (UUID(int=87000), UUID(int=87001), "user.large"),
            (UUID(int=87002), UUID(int=87003), "user.history_bound"),
        ]:
            session.add(
                EventDefinition(
                    id=definition,
                    owner_id=owner,
                    namespace="user",
                    key=key,
                    status="active",
                    current_version=1,
                )
            )
            session.flush()
            session.add(
                EventDefinitionVersion(
                    id=version,
                    definition_id=definition,
                    version=1,
                    schema={},
                    schema_hash="0" * 64,
                    topology="point",
                    field_metadata={},
                    labels={},
                    privacy="private",
                    allowed_operations=["query"],
                )
            )
            session.flush()
        payload = {f"part{index}": "Я" * 4000 for index in range(4)}
        for index in range(1000):
            row(
                session,
                UUID(int=88000 + index),
                "user.large",
                UUID(int=87001),
                payload=payload,
                topology="point",
            )
        session.flush()
        session.execute(
            text("""
            INSERT INTO events(id,kind,definition_version_id,start,timezone,source,confidence,status,payload,
                topology,envelope_version,time_precision,assertion_kind,producer,evidence_refs,validation_status,
                revision,deleted,recorded_at,ingested_at,updated_at)
            SELECT ('00000000-0000-0000-0000-'||lpad(to_hex(100000+index),12,'0'))::uuid,
                'user.history_bound',:version,:start,'UTC','manual',1,'confirmed',
                '{"description":"Synthetic bounded history"}'::jsonb,'point',1,'instant','user_report','owner',
                '[]','schema_validated',1,false,:ingested,
                CASE WHEN index=0 THEN :ingested ELSE :future END,
                CASE WHEN index=0 THEN :ingested ELSE :future END
            FROM generate_series(0,9999) AS parts(index)
        """),
            {"version": UUID(int=87003), "start": AT, "ingested": T0, "future": T2},
        )


def run(binary):
    engine = isolated_engine()
    try:
        owner = seed(engine)
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "entries.json"
            for phase in [
                "no_grant",
                "schema",
                "facts",
                "health_only",
                "diary_only",
                "migraine_off",
                "replay",
            ]:
                with Session(engine) as session, session.begin():
                    categories = (
                        ["health"]
                        if phase == "health_only"
                        else ["diary"]
                        if phase == "diary_only"
                        else ["health", "diary"]
                    )
                    session.execute(
                        text(
                            "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                        ),
                        {"value": json.dumps({"model_categories": categories})},
                    )
                    session.execute(text("UPDATE module_configs SET llm_enabled=true"))
                    session.execute(
                        text("DELETE FROM app_state WHERE key LIKE 'tracker-consent:%'")
                    )
                    if phase in {"schema", "facts", "health_only", "diary_only"}:
                        consent = TrackerShareConsent(
                            definition_id=TRACKER,
                            destination_kind="model",
                            destination_instance_id="model:mcp:local",
                            categories={"schema"} if phase == "schema" else {"facts"},
                            granted_at=datetime(2020, 1, 1, tzinfo=UTC),
                        )
                        session.add(
                            AppState(
                                key=f"tracker-consent:{TRACKER}:model:model:mcp:local",
                                value=consent.model_dump(mode="json"),
                            )
                        )
                    if phase == "migraine_off":
                        session.execute(
                            text(
                                "UPDATE module_configs SET llm_enabled=false WHERE pack_key='migraine'"
                            )
                        )
                if phase == "replay":
                    with Session(engine) as session, session.begin():
                        session.add(
                            SourcePayload(
                                source="garmin_connect",
                                endpoint="daily",
                                source_key="2026-10-25",
                                payload_hash="b" * 64,
                                archive_key="sha256:" + "b" * 64,
                                fetched_at=T0,
                                parser_version=0,
                                status="pending",
                                payload={"totalSteps": 0},
                            )
                        )
                values = capture(engine, cases(owner))
                fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
                subprocess.run(
                    [binary, "--ignored", "--exact", "compares_generic_entry_history"],
                    check=True,
                    env={**os.environ, "GA_TEST_GENERIC_ENTRIES": str(fixture)},
                )
                print("Matched entry history phase", phase, "cases", len(values))
            with Session(engine) as session, session.begin():
                session.execute(
                    text(
                        "DELETE FROM source_payloads WHERE source='garmin_connect' AND endpoint='daily'"
                    )
                )
                session.execute(
                    text(
                        'UPDATE app_state SET value=\'{"model_categories":["health","diary"]}\'::jsonb WHERE key=\'preferences:onboarding\''
                    )
                )
                session.execute(text("UPDATE module_configs SET llm_enabled=true"))
            seed_large(engine)
            large = [
                {"name": "large-owner", "spec": arguments("user.large", limit=1000)},
                {
                    "name": "large-mcp",
                    "owner": str(owner),
                    "spec": arguments("user.large", limit=1000),
                },
            ]
            values = capture(engine, large)
            fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored", "--exact", "compares_generic_entry_history"],
                check=True,
                env={**os.environ, "GA_TEST_GENERIC_ENTRIES": str(fixture)},
            )
            print("Matched 1000 complete entry payloads for owner and MCP")
            boundary = [
                {"name": "exactly-10000-candidates", "spec": arguments("user.history_bound", T1)}
            ]
            values = capture(engine, boundary)
            fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored", "--exact", "compares_generic_entry_history"],
                check=True,
                env={**os.environ, "GA_TEST_GENERIC_ENTRIES": str(fixture)},
            )
            with Session(engine) as session, session.begin():
                row(
                    session,
                    UUID(int=120000),
                    "user.history_bound",
                    UUID(int=87003),
                    ingested=T2,
                    updated=T2,
                )
            values = capture(
                engine, [{"name": "10001-candidates", "spec": arguments("user.history_bound", T1)}]
            )
            fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored", "--exact", "compares_generic_entry_history"],
                check=True,
                env={**os.environ, "GA_TEST_GENERIC_ENTRIES": str(fixture)},
            )
            print("Matched exact 10000/10001 candidate history boundary and recovery")
            with Session(engine) as session, session.begin():
                consent = TrackerShareConsent(
                    definition_id=TRACKER,
                    destination_kind="model",
                    destination_instance_id="model:mcp:local",
                    categories={"facts"},
                    granted_at=datetime(2020, 1, 1, tzinfo=UTC),
                )
                session.merge(
                    AppState(
                        key=f"tracker-consent:{TRACKER}:model:model:mcp:local",
                        value=consent.model_dump(mode="json"),
                    )
                )
            database_name = engine.dialect.identifier_preparer.quote(engine.url.database)
            try:
                for zone, role_zone in [
                    ("America/New_York", None),
                    ("Asia/Kathmandu", None),
                    ("America/New_York", "Asia/Kathmandu"),
                ]:
                    with engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT"
                    ) as connection:
                        connection.execute(
                            text(f"ALTER DATABASE {database_name} SET timezone TO '{zone}'")
                        )
                        if role_zone:
                            connection.execute(
                                text(
                                    f"ALTER ROLE CURRENT_USER IN DATABASE {database_name} "
                                    f"SET timezone TO '{role_zone}'"
                                )
                            )
                    engine.dispose()
                    values = capture(engine, cases(owner))
                    fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
                    subprocess.run(
                        [binary, "--ignored", "--exact", "compares_generic_entry_history"],
                        check=True,
                        env={**os.environ, "GA_TEST_GENERIC_ENTRIES": str(fixture)},
                    )
                    print(
                        "Matched stored entry displays and historic timezone offsets in",
                        role_zone or zone,
                    )
            finally:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    connection.execute(
                        text(f"ALTER ROLE CURRENT_USER IN DATABASE {database_name} RESET timezone")
                    )
                    connection.execute(text(f"ALTER DATABASE {database_name} RESET timezone"))
                engine.dispose()

    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
