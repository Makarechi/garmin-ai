"""Compare persisted source records, corrections and cursors with pinned Python."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from garmin_ai import source_runtime
from garmin_ai.accounts import owner
from garmin_ai.models import AppState, MetricObservation, SourcePayload
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord
from garmin_ai.source_metric_mapping import (
    SourceMetricMappingConfirmation,
    confirm_source_metric_mapping,
    preview_source_metric_mapping,
)

PINNED_TESTS = Path("/work/tests")
if PINNED_TESTS.is_dir():
    sys.path.insert(0, str(PINNED_TESTS))

from test_source_metric_mapping import install_metric, mapping, settings  # noqa: E402

INSTANCE = "source:sample:one"
NOW = datetime(2026, 3, 30, 20, tzinfo=UTC)
CAPABILITIES = SourceCapabilities(corrections=True, deletions=True, cursor=True)


def record(identity, value=None, unit="minutes", operation="upsert"):
    payload = {"walk_minutes": value, "unit": unit} if operation == "upsert" else {}
    return SourceRecord(
        source_record_id=identity,
        operation=operation,
        observed_at=NOW - timedelta(hours=1),
        effective_at=datetime.fromisoformat("2026-03-30T17:12:01+02:00"),
        source_timezone="Europe/Bratislava",
        source_reference=f"synthetic:{identity}",
        payload=payload,
    )


def page(offset, *records, cursor=None, retry=None):
    return SourcePage(
        instance_id=INSTANCE,
        page_kind="partial",
        fetched_at=NOW + timedelta(minutes=offset),
        records=records,
        next_cursor=cursor,
        retry_after=NOW + timedelta(minutes=retry) if retry is not None else None,
    )


def steps():
    return [
        (
            0,
            None,
            page(0, record("a", 12), record("missing-unit", 21, None), cursor="c-1", retry=10),
            "deferred",
        ),
        (4, None, None, "waiting"),
        (11, "c-1", page(11, record("a", 18), record("raw-big-number", 2**100)), "complete"),
        (12, None, page(12, record("a", 18)), "complete"),
        (13, None, page(13, record("a", 28, "hours")), "complete"),
        (14, None, page(14, record("a", operation="delete")), "complete"),
        (15, None, page(15, record("rollback-a", 1), record("rollback-z", 2)), "error"),
        (16, None, page(16, record("a", 14)), "complete"),
        (17, None, page(17, record("a", operation="delete")), "complete"),
    ]


def seed(engine):
    configured = settings()
    with Session(engine) as session, session.begin():
        owner(session)
        metric = install_metric(session)
        draft = mapping(metric.id)
        preview = preview_source_metric_mapping(session, configured, draft)
        confirm_source_metric_mapping(
            session,
            configured,
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=preview["confirmation_token"]
            ),
        )
        session.add(
            AppState(
                key="analysis:conversation", value={"epoch": "old", "turns": [{"synthetic": True}]}
            )
        )
        session.add(AppState(key="analysis:conversation:pending:synthetic", value={"turn": {}}))
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE source_payloads ADD CHECK (source_key != 'rollback-z')")
        )
    with Session(engine) as session:
        key = session.scalar(
            select(AppState).where(AppState.key.startswith("source-metric-mapping:"))
        )
        return key.value["source_hash"]


class FixtureSource:
    capabilities = CAPABILITIES

    def __init__(self, cursor, page):
        self.cursor = cursor
        self.page = page

    def read_page(self, *, start, end, cursor, limit):
        assert cursor == self.cursor
        assert (end - start).days == 7
        assert limit == 100
        assert self.page is not None
        return self.page

    def close(self):
        pass


def write_python(engine, fixture):
    for offset, expected_cursor, page_value, expected in fixture:
        adapter = FixtureSource(expected_cursor, page_value)
        registry = SimpleNamespace(
            descriptor=lambda *_: SimpleNamespace(plugin_factory=FixtureSource),
            status=lambda *_: SimpleNamespace(available=True),
            create=lambda *_, selected=adapter: selected,
        )
        with patch.object(source_runtime, "default_registry", return_value=registry):
            try:
                result = source_runtime.poll_source_instance(
                    engine, settings(), INSTANCE, now=NOW + timedelta(minutes=offset)
                )
            except IntegrityError:
                assert expected == "error"
            else:
                assert result["status"] == ("deferred" if expected == "waiting" else expected)


def snapshot(engine):
    with Session(engine) as session:
        raw = {}
        raw_ids = {}
        for entry in session.scalars(select(SourcePayload)):
            raw_ids[entry.id] = entry.payload_hash
            raw[entry.payload_hash] = (
                entry.source,
                entry.endpoint,
                entry.source_key,
                entry.payload,
                entry.archive_key,
                entry.fetched_at,
                entry.source_updated_at,
                entry.parser_version,
                entry.status,
            )
        observations = {}
        for observation in session.scalars(select(MetricObservation)):
            observations[observation.id] = (
                raw_ids[observation.source_ref],
                observation.metric,
                observation.value,
                observation.unit,
                observation.observed_at,
                observation.effective_start,
                observation.recorded_at,
                observation.uploaded_at,
                observation.fetched_at,
                str(observation.source_calendar_date),
                observation.timezone,
                observation.quality,
                observation.account,
                observation.feature_version,
                observation.sequence,
                observation.valid,
                observation.invalidated_at is not None,
            )
        states = {}
        for entry in session.scalars(select(AppState)):
            if entry.key.startswith("source-plugin:") or entry.key.startswith(
                "analysis:conversation"
            ):
                states[entry.key] = entry.value
            elif entry.key.startswith("source-metric-projection:"):
                fields = dict(entry.value)
                if fields["observation_id"] is not None:
                    reference = UUID(fields["observation_id"])
                    fields["observation_id"] = observations[reference][:3]
                states[entry.key.rsplit(":", 1)[-1]] = fields
            elif entry.key.startswith("source-metric-mapping:"):
                states["mapping_status"] = entry.value["state"]
            elif entry.key == "replay:generation":
                states[entry.key] = bool(UUID(entry.value["id"]))
        return raw, sorted(observations.values()), states


def run(binary):
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    rust_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_url == python_url:
        raise ValueError("Use two distinct disposable databases")
    fixture = steps()
    python = isolated_engine()
    try:
        seed(python)
        write_python(python, fixture)
        os.environ["GA_TEST_DATABASE_URL"] = rust_url
        rust = isolated_engine()
        try:
            config_hash = seed(rust)
            data = {
                "instance": INSTANCE,
                "config_hash": config_hash,
                "capabilities": CAPABILITIES.model_dump(mode="json"),
                "steps": [
                    {
                        "now": (NOW + timedelta(minutes=offset)).isoformat(),
                        "cursor": cursor,
                        "expected": expected,
                        "wire": page.model_dump_json() if page is not None else None,
                        "verify_stale": offset == 12,
                    }
                    for offset, cursor, page, expected in fixture
                ],
            }
            with TemporaryDirectory() as scratch:
                path = Path(scratch) / "source-pages.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                subprocess.run(
                    [binary, "--ignored"],
                    env={**os.environ, "GA_TEST_SOURCE_PAGES": str(path)},
                    check=True,
                )
            expected = snapshot(python)
            actual = snapshot(rust)
            for index, (before, after) in enumerate(zip(expected, actual, strict=True)):
                assert before == after, (index, before, after)
            print("Source raws, corrected metrics, revision history and saved cursors match Python")
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
