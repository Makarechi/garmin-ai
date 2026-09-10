from datetime import UTC, datetime, timedelta

from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState, Job, SourcePayload
from garmin_ai.observability import prometheus, snapshot

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def test_due_queue_age_excludes_future_and_completed_work(db):
    enqueue(db, "telegram_update", {}, "synthetic-one", NOW - timedelta(minutes=5))
    enqueue(db, "telegram_update", {}, "synthetic-two", NOW + timedelta(minutes=5))
    identity = enqueue(db, "garmin_endpoint", {}, "synthetic-done", NOW - timedelta(days=2))
    db.get(Job, identity).status = "done"
    enqueue(db, "garmin_endpoint", {}, "synthetic-three", NOW - timedelta(seconds=60))
    db.flush()
    rows = {row["lane"]: row for row in snapshot(db, NOW)["queue_due"]}
    assert rows["telegram"]["count"] == 1
    assert rows["telegram"]["oldest_due_age_seconds"] == 300
    assert rows["garmin"]["oldest_due_age_seconds"] == 60


def test_metrics_never_emit_arbitrary_database_labels_or_payload(db):
    marker = "synthetic-private-health-text"
    identity = enqueue(db, marker, {"notes": marker}, "synthetic", NOW)
    db.get(Job, identity).status = marker
    db.add(
        SourcePayload(
            endpoint=marker,
            source_key=marker,
            payload_hash="synthetic",
            payload={"text": marker},
            archive_key=marker,
            status=marker,
            fetched_at=NOW,
        )
    )
    db.add(AppState(key="integration:garmin", value={"status": marker, "reason_class": marker}))
    db.flush()
    assert marker not in str(snapshot(db, NOW))
    text = prometheus(db)
    assert marker not in text
    assert 'kind="other",status="other"' in text
    assert 'endpoint="other",status="other"' in text


def test_connection_gate_is_reported_independently_of_heartbeat(db):
    db.add(AppState(key="runtime:heartbeat", value={"at": NOW.isoformat()}))
    row = AppState(key="integration:garmin", value={"status": "reauth_required"})
    db.add(row)
    db.flush()
    result = snapshot(db, NOW)
    assert result["runtime_heartbeat_age_seconds"] == 0
    assert result["garmin_connection"] == {"state": "reauth_required", "paused": True}
    row.value = {
        "status": "rate_limited",
        "blocked_until": (NOW - timedelta(seconds=1)).isoformat(),
    }
    db.flush()
    assert not snapshot(db, NOW)["garmin_connection"]["paused"]
