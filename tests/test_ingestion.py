import io
import zipfile
from datetime import UTC, datetime

import pytest
from fitdecode.exceptions import FitHeaderError
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.fit import extract_fit, parse_fit
from garmin_ai.ingest import ingest
from garmin_ai.models import Activity, HealthDay, Measurement, SourcePayload, TimelineInterval


def test_upstream_replay_corrections_and_empty_preserve_history(db, tmp_path):
    archive = LocalArchive(tmp_path)

    def put(payload):
        return ingest(db, archive, "daily", "2026-09-07", payload, "Europe/Bratislava")

    a = {"totalSteps": 100, "restingHeartRate": 52}
    assert put(a)["status"] == "normalized"
    assert put(a)["status"] == "unchanged"
    assert db.scalar(select(func.count()).select_from(SourcePayload)) == 1
    put({"totalSteps": 200})
    db.expire_all()
    row = db.scalar(select(HealthDay))
    assert row.steps == 200 and row.resting_hr == 52
    put({})
    db.expire_all()
    assert row.steps == 200
    put(a)
    db.expire_all()
    assert row.steps == 100


def test_midnight_sleep_and_dst_samples(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "sleep",
        "2026-10-25",
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": 1792879200000,
                "sleepEndTimestampGMT": 1792911600000,
                "sleepTimeSeconds": 32400,
            }
        },
        "Europe/Bratislava",
    )
    sleep = db.scalar(select(TimelineInterval))
    assert sleep.end > sleep.start
    # Repeated local 02:30 during autumn DST is two distinct UTC instants.
    values = [
        [datetime(2026, 10, 25, hour, 30, tzinfo=UTC).timestamp() * 1000, 60] for hour in (0, 1)
    ]
    values += [[values[0][0] + 1000, -1], [values[0][0] + 2000, None]]
    ingest(
        db, archive, "heart_rate", "2026-10-25", {"heartRateValues": values}, "Europe/Bratislava"
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 2


def test_partial_parse_failure_preserves_raw_and_rolls_back_canonical(db, tmp_path):
    archive = LocalArchive(tmp_path)
    payload = {"heartRateValues": [[1792886400000, 70], ["bad-timestamp", 80]]}
    result = ingest(db, archive, "heart_rate", "2026-10-25", payload, "Europe/Bratislava")
    assert result["status"] == "error"
    assert db.scalar(select(func.count()).select_from(Measurement)) == 0
    raw = db.scalar(select(SourcePayload))
    assert raw.payload == payload and raw.status == "error"


def test_activity_identity_and_readiness_zero(db, tmp_path):
    archive = LocalArchive(tmp_path)
    activity = {
        "activityId": 1,
        "startTimeGMT": "2026-09-06 23:30:00",
        "duration": 3600,
        "distance": 10000,
        "activityType": {"typeKey": "running"},
    }
    ingest(db, archive, "activities", "recent", [activity], "Europe/Bratislava")
    ingest(db, archive, "activity", "1", activity, "Europe/Bratislava")
    assert db.scalar(select(func.count()).select_from(Activity)) == 1
    row = db.get(Activity, "1")
    assert row.start.day == 6 and row.end.day == 7
    ingest(
        db,
        archive,
        "readiness",
        "2026-09-07",
        [{"score": 80, "recoveryTime": 180, "recoveryTimeChangePhrase": "REACHED_ZERO"}],
        "Europe/Bratislava",
    )
    assert db.scalar(select(HealthDay)).recovery_time_minutes == 0


def test_fit_failure_and_no_zip_path_extraction(tmp_path):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("../../escape.fit", b"corrupt fit")
    assert extract_fit(raw.getvalue()) == [b"corrupt fit"]
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(FitHeaderError):
        parse_fit(b"corrupt fit")
