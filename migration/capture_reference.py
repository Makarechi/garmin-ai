"""Capture synthetic contract cases using the pinned Python application and a fresh test DB."""

import argparse
import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from replay_reference import capture_replay
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.archive import LocalArchive
from garmin_ai.config import ApiToken, Settings
from garmin_ai.events import serialize
from garmin_ai.models import Audit, Event

BASELINE_SHA = "ee3a14a48a6ad30c1085951bac2f53c9088c2450"
MIGRATION = Path(__file__).resolve().parent
TIME_FIELDS = {"recorded_at", "ingested_at", "created_at", "updated_at"}


def isolated_engine():
    url = make_url(os.environ["GA_TEST_DATABASE_URL"])
    if (
        url.get_backend_name() != "postgresql"
        or not (url.database or "").endswith("_test")
        or url.query
    ):
        raise ValueError("A fresh, dedicated PostgreSQL database ending in _test is required")
    engine = create_engine(url, hide_parameters=True)
    # Extension-internal relations may exist. Apply the pinned schema only to an empty public schema.
    if inspect(engine).get_table_names(schema="public"):
        engine.dispose()
        raise ValueError("The differential reference database must start empty")
    try:
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except BaseException:
        engine.dispose()
        raise
    return engine


def archive_case(entries, directory):
    archive = LocalArchive(directory)
    result = {}
    for entry in entries:
        payload = entry["payload"]
        key = archive.put_json(payload)
        result[entry["name"]] = {
            "key": key,
            "bytes_base64": base64.b64encode(archive.read(key)).decode("ascii"),
            "repeat_key": archive.put_json(payload),
        }
    result["file_count"] = len(list(directory.glob("*/*.json")))
    try:
        archive.put_json({"value": float("nan")})
    except ValueError:
        result["accepted_nonfinite"] = False
    else:
        result["accepted_nonfinite"] = True
    result["file_count_after_nonfinite"] = len(list(directory.glob("*/*.json")))
    return result


def cli_cases(cases, directory):
    executable = str(Path(sys.executable).with_name("garmin-ai"))
    results = {}
    for case in cases:
        process = subprocess.run(
            [executable, *case["args"]],
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        results[case["name"]] = {
            "status": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
    return results


class EventObservation:
    """Replace generated tokens with stable names while preserving their equality."""

    def __init__(self):
        self.ids = {}
        self.timestamps = {}

    def event(self, value):
        normalized = dict(value)
        for key in ("id", "definition_version_id"):
            original = normalized.get(key)
            if original is None:
                continue
            UUID(original)
            normalized[key] = self.ids.setdefault(original, f"uuid:{len(self.ids) + 1}")
        for key in sorted(TIME_FIELDS & value.keys()):
            original = normalized[key]
            if original is None:
                continue
            if datetime.fromisoformat(original).utcoffset() is None:
                raise ValueError(f"Naive server timestamp for {key}")
            normalized[key] = self.timestamps.setdefault(
                original, f"time:{len(self.timestamps) + 1}"
            )
        if "canonical" in value:
            normalized["canonical"] = self.event(value["canonical"])
        return normalized


async def mcp_case(engine, directory, observation):
    config = StdioServerParameters(
        command=str(Path(sys.executable).with_name("garmin-ai")),
        args=["mcp"],
        env={
            "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "GA_MCP_ENABLE_WRITES": "false",
            "GA_DATA_DIR": str(directory / "data"),
            "GA_TOKEN_DIR": str(directory / "tokens"),
            "GA_LOCK_DIR": str(directory / "locks"),
            "GA_BACKUP_DIR": str(directory / "backups"),
        },
        cwd=directory,
    )
    async with (
        asyncio.timeout(15),
        stdio_client(config) as (reader, writer),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        listing = await client.list_tools()
        result = await client.call_tool(
            "events",
            {"start": "2026-10-25T00:00:00Z", "end": "2026-10-26T00:00:00Z"},
        )
        if result.isError or result.structuredContent is None:
            raise RuntimeError("The MCP events read failed")
        if json.loads(result.content[0].text) != result.structuredContent:
            raise RuntimeError("MCP text and structured event results diverged")
        invalid = await client.call_tool("events", {"start": "not-a-date", "end": "missing"})
        attempted_write = await client.call_tool("events_create", {})
    return {
        "tools": [
            {"name": tool.name, "annotations": tool.annotations.model_dump(exclude_none=True)}
            for tool in listing.tools
        ],
        "read": {
            **result.structuredContent,
            "rows": [observation.event(row) for row in result.structuredContent["rows"]],
        },
        "invalid_input": invalid.model_dump(mode="json", exclude_none=True),
        "disabled_write": attempted_write.model_dump(mode="json", exclude_none=True),
    }


def diary_case(engine, directory, body):
    # Per-run synthetic bearer values never enter the observation.
    admin = secrets.token_urlsafe(32)
    reader = secrets.token_urlsafe(32)
    settings = Settings(
        _env_file=None,
        api_tokens=[
            ApiToken(key=admin, scopes={"admin"}),
            ApiToken(key=reader, scopes={"read:diary"}),
        ],
        data_dir=directory / "data",
        token_dir=directory / "tokens",
        lock_dir=directory / "locks",
        backup_dir=directory / "backups",
    )
    auth = {"Authorization": f"Bearer {admin}"}
    retry = {**auth, "Idempotency-Key": "synthetic-differential-note"}
    readonly = {"Authorization": f"Bearer {reader}"}
    observation = EventObservation()
    responses = {}

    def record(name, response):
        value = response.json()
        if response.is_success and "idempotency_key" in value:
            value = observation.event(value)
        responses[name] = {"status": response.status_code, "body": value}

    with TestClient(create_app(settings, engine)) as client:
        record("without_token", client.post("/events", json=body))
        record("read_scope_cannot_write", client.post("/events", headers=readonly, json=body))
        record(
            "missing_payload", client.post("/events", headers=auth, json={"start": body["start"]})
        )
        created = client.post("/events", json=body, headers=retry)
        created.raise_for_status()
        record("create", created)
        identity = created.json()["id"]
        record("retry", client.post("/events", json=body, headers=retry))
        conflicting = {**body, "payload": {"type": "note", "description": "Conflicting fixture"}}
        record("conflicting_retry", client.post("/events", json=conflicting, headers=retry))
        record(
            "stale_revision",
            client.put(f"/events/{identity}", json={"revision": 7, "event": body}, headers=auth),
        )
    # Open a new connection pool and app instance, retaining only the persisted database.
    engine.dispose()
    with TestClient(create_app(settings, engine)) as restarted:
        record("read_after_restart", restarted.get(f"/events/{identity}", headers=readonly))

    mcp_result = asyncio.run(mcp_case(engine, directory, observation))
    with Session(engine) as session:
        events = [observation.event(serialize(row)) for row in session.scalars(select(Event))]
        audits = [
            {
                **{field: getattr(row, field) for field in ("id", "action", "actor")},
                "event_id": observation.ids[str(row.event_id)],
                "before": observation.event(row.before) if row.before is not None else None,
                "after": observation.event(row.after),
                "operation_id": str(row.operation_id) if row.operation_id is not None else None,
                "created_at": observation.timestamps[row.created_at.isoformat()],
            }
            for row in session.scalars(select(Audit).order_by(Audit.id))
        ]
        counts = {
            "events": session.scalar(select(func.count()).select_from(Event)),
            "audit": session.scalar(select(func.count()).select_from(Audit)),
        }
    return {
        "responses": responses,
        "events": events,
        "audit": audits,
        "mcp": mcp_result,
        "row_counts": counts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=MIGRATION / "contracts/initial-cases.json")
    options = parser.parse_args()
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if source != BASELINE_SHA:
        raise ValueError("Run the Python capture from the pinned baseline checkout")
    cases = json.loads(options.cases.read_text(encoding="utf-8"))
    if cases["format_version"] != 1:
        raise ValueError("Unexpected case format")
    # No local application configuration or live provider variables may reach the app.
    database_url = os.environ["GA_TEST_DATABASE_URL"]
    os.environ.clear()
    os.environ["GA_TEST_DATABASE_URL"] = database_url
    engine = isolated_engine()
    try:
        with tempfile.TemporaryDirectory() as private:
            directory = Path(private)
            result = {
                "baseline_sha": source,
                "format_version": 1,
                "implementation": {"language": "python", "revision": source},
                "scenarios": {
                    "archive": archive_case(cases["archive_values"], directory / "archive"),
                    "cli": cli_cases(cases["cli_cases"], directory),
                    "diary": diary_case(engine, directory, cases["diary_event"]),
                    "replay": capture_replay(engine, directory / "replay", cases["replay"]),
                },
            }
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
