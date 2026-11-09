import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select, update

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import _journal_rebuilt_measurements, ingest
from garmin_ai.models import Measurement, MeasurementRevision

NOW = datetime(2026, 9, 10, tzinfo=UTC)


@pytest.mark.parametrize("count", [20, 501])
@pytest.mark.parametrize("null_source_ref", [False, True])
def test_rebuilt_journal_batches_preserve_changes_deletions_and_retry_identity(
    db, tmp_path, monkeypatch, count, null_source_ref
):
    ingest(
        db,
        LocalArchive(tmp_path),
        "heart_rate",
        str(NOW.date()),
        {
            "heartRateValues": [
                [int((NOW + timedelta(minutes=i)).timestamp() * 1000), 70] for i in range(count)
            ]
        },
        "UTC",
        fetched_at=NOW,
    )
    if null_source_ref:
        db.execute(update(Measurement).values(source_ref=None))
    previous = [
        {column.name: getattr(row, column.name) for column in Measurement.__table__.columns}
        for row in db.scalars(select(Measurement).order_by(Measurement.ts))
    ]
    db.execute(update(Measurement).where(Measurement.ts == NOW).values(value=80))
    db.execute(delete(Measurement).where(Measurement.ts == NOW + timedelta(minutes=1)))

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(days=1)

    monkeypatch.setattr(importlib.import_module("garmin_ai.ingest"), "datetime", FixedClock)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if "measurement" in statement.lower():
            statements.append(statement)

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        for _ in range(2):
            _journal_rebuilt_measurements(db, previous, NOW + timedelta(hours=1))
    finally:
        event.remove(connection, "before_cursor_execute", capture)

    rebuilt_at = NOW + timedelta(days=1, microseconds=1)
    revisions = db.scalars(
        select(MeasurementRevision)
        .where(MeasurementRevision.ingested_at == rebuilt_at)
        .order_by(MeasurementRevision.ts)
    ).all()
    assert len(revisions) == count
    assert (revisions[0].value, revisions[0].deleted) == (80, False)
    assert (revisions[1].value, revisions[1].deleted) == (70, True)
    assert all(row.value == 70 and not row.deleted for row in revisions[2:])
    assert all((row.source_ref is None) == null_source_ref for row in revisions)
    assert len(statements) <= 5 * ((count + 499) // 500) + 2
