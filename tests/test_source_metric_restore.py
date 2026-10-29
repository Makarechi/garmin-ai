"""Source mappings and revision identities survive a synthetic export and restore."""

from datetime import UTC, datetime, timedelta

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
from garmin_ai.models import Base, MetricObservation
from garmin_ai.operations import export_database, restore_database
from garmin_ai.source_runtime import poll_source_instance


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
