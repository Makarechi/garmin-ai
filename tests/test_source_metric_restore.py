"""Source mappings and revision identities survive a synthetic export and restore."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_source_metric_mapping import install_metric
from test_source_metric_projection import (
    configure,
    confirm_mapping,
    source_record,
    source_registry,
)

from garmin_ai.metric_definitions import aggregate_metric
from garmin_ai.models import AppState, Base, MetricObservation, SourcePayload
from garmin_ai.operations import export_database, restore_database
from garmin_ai.source_contracts import SourceCapabilities, SourcePage
from garmin_ai.source_runtime import poll_source_instance
from garmin_ai.tools import call_tool


def test_source_revisions_remain_idempotent_after_restore(db, db_engine, monkeypatch, tmp_path):
    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now, 15)}
    source_registry(monkeypatch, contents)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)

    backup = tmp_path / "synthetic-source-export.jsonl.gz"
    counts = export_database(db_engine, backup)
    db.rollback()
    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    assert restore_database(db_engine, backup) == counts

    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    with Session(db_engine) as restored:
        assert [row.value for row in restored.scalars(select(MetricObservation)).all()] == [15]
        result = aggregate_metric(
            restored, "user.walk.minutes", now - timedelta(days=1), now + timedelta(seconds=1)
        )
        assert result["value"] == 15
    contents["source:sample:one"] = source_record(now, 25)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    with Session(db_engine) as restored:
        rows = restored.scalars(
            select(MetricObservation).order_by(MetricObservation.sequence)
        ).all()
        assert [(row.value, row.valid) for row in rows] == [(15, False), (25, True)]
    contents["source:sample:one"] = source_record(now, operation="delete")
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    with Session(db_engine) as restored:
        rows = restored.scalars(select(MetricObservation)).all()
        assert len(rows) == 2 and not any(row.valid for row in rows)


def test_source_page_error_rolls_back_earlier_projections(db, db_engine, monkeypatch):
    from garmin_ai import source_runtime

    metric = install_metric(db)
    configured = configure("source:sample:one")
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {"fetched_at": now, "source:sample:one": source_record(now, 15)}
    capabilities = SourceCapabilities(corrections=False, deletions=True)
    source_registry(monkeypatch, contents, capabilities)
    poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    cursor_before = dict(db.get(AppState, "source-plugin:cursor:source:sample:one").value)
    db.rollback()

    registry = source_runtime.default_registry()
    adapter = registry.create(configured.integrations[0], configured)
    new_record = source_record(now, 10).model_copy(
        update={"source_record_id": "another-synthetic-record"}
    )
    page = SourcePage(
        instance_id="source:sample:one",
        page_kind="partial",
        fetched_at=now,
        records=[new_record, source_record(now, 25)],
    )
    monkeypatch.setattr(adapter, "read_page", lambda **_kwargs: page)
    monkeypatch.setattr(registry, "create", lambda *_args: adapter)
    monkeypatch.setattr(source_runtime, "default_registry", lambda *_args: registry)
    with pytest.raises(ValueError, match="without correction capability"):
        poll_source_instance(db_engine, configured, "source:sample:one", now=now)
    db.expire_all()
    rows = db.scalars(select(MetricObservation)).all()
    assert len(rows) == 1 and rows[0].value == 15 and rows[0].valid
    assert len(db.scalars(select(SourcePayload)).all()) == 1
    assert db.get(AppState, "source-plugin:cursor:source:sample:one").value == cursor_before


def test_source_selection_prevents_aggregation_across_instances(db, db_engine, monkeypatch):
    metric = install_metric(db)
    configured = configure("source:sample:one", "source:sample:two")
    for instance in ("source:sample:one", "source:sample:two"):
        confirm_mapping(db, configured, instance, metric.id)
    db.commit()
    now = datetime.now(UTC)
    contents = {
        "fetched_at": now,
        "source:sample:one": source_record(now, 15),
        "source:sample:two": source_record(now, 25),
    }
    source_registry(monkeypatch, contents)
    for instance in ("source:sample:one", "source:sample:two"):
        poll_source_instance(db_engine, configured, instance, now=now)
    db.expire_all()
    start, end = now - timedelta(days=1), now + timedelta(seconds=1)
    with pytest.raises(ValueError, match="Multiple metric sources"):
        aggregate_metric(db, "user.walk.minutes", start, end)
    result = call_tool(
        db,
        "generic_analysis",
        {
            "spec": {
                "operation": "query_observations",
                "metric_key": "user.walk.minutes",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "knowledge_cutoff": datetime.now(UTC).isoformat(),
            }
        },
    )
    for row in result["rows"]:
        selected = aggregate_metric(
            db, "user.walk.minutes", start, end, source=row["metric_source"]
        )
        assert selected["value"] == row["value"]
    assert len(result["rows"]) == 2
