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


def test_actual_stress_descriptor_and_hourly_spo2_shapes(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "stress",
        "2026-09-07",
        {
            "bodyBatteryValueDescriptorsDTOList": [
                {
                    "bodyBatteryValueDescriptorIndex": 2,
                    "bodyBatteryValueDescriptorKey": "bodyBatteryLevel",
                }
            ],
            "bodyBatteryValuesArray": [[1788782400000, "MEASURED", 75, 5.0]],
        },
        "Europe/Bratislava",
    )
    ingest(
        db,
        archive,
        "spo2",
        "2026-09-07",
        {"spO2HourlyAverages": [[1788782400000, 98]]},
        "Europe/Bratislava",
    )
    assert db.scalar(select(Measurement.value).where(Measurement.metric == "body_battery")) == 75
    assert db.scalar(select(Measurement.value).where(Measurement.metric == "spo2_pct")) == 98


def test_stale_fetch_does_not_overwrite_newer_data(db, tmp_path):
    archive = LocalArchive(tmp_path)
    now = datetime.now(UTC)
    ingest(db, archive, "daily", "2026-09-07", {"totalSteps": 200}, "UTC", fetched_at=now)
    from datetime import timedelta

    result = ingest(
        db,
        archive,
        "daily",
        "2026-09-07",
        {"totalSteps": 100},
        "UTC",
        fetched_at=now - timedelta(seconds=10),
    )
    assert result["status"] == "stale"
    assert db.scalar(select(HealthDay)).steps == 200


def test_failed_fit_retains_searchable_activity_source(db, tmp_path):
    from garmin_ai.fit import store_fit

    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "activity",
        "1",
        {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100},
        "UTC",
    )
    result = store_fit(db, archive, "1", b"bad fit")
    db.flush()
    assert result["status"] == "error"
    raw = db.scalar(select(SourcePayload).where(SourcePayload.endpoint == "activity_fit"))
    assert raw.status == "error" and archive.read(raw.archive_key) == b"bad fit"
    assert db.get(Activity, "1").fit_key == raw.archive_key


def test_corrected_samples_replace_and_field_sources_survive(db, tmp_path):
    archive = LocalArchive(tmp_path)
    a = ingest(
        db, archive, "daily", "2026-09-07", {"totalSteps": 100, "restingHeartRate": 50}, "UTC"
    )
    b = ingest(db, archive, "daily", "2026-09-07", {"totalSteps": 200}, "UTC")
    day = db.scalar(select(HealthDay))
    assert day.sources["field:resting_hr"] == a["source_ref"]
    assert day.sources["field:steps"] == b["source_ref"]
    for points in ([[1788782400000, 60], [1788782460000, 70]], [[1788782400000, 65]]):
        ingest(db, archive, "heart_rate", "2026-09-07", {"heartRateValues": points}, "UTC")
    assert db.scalar(select(func.count()).select_from(Measurement)) == 1
    assert db.scalar(select(Measurement.value)) == 65


def test_partial_activity_keeps_known_timezone_and_kind(db, tmp_path):
    archive = LocalArchive(tmp_path)
    base = {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100}
    ingest(
        db,
        archive,
        "activity",
        "1",
        {
            **base,
            "activityType": {"typeKey": "running"},
            "timeZoneUnitDTO": {"timeZone": "America/New_York"},
        },
        "UTC",
    )
    ingest(db, archive, "activity", "1", {**base, "duration": 200}, "UTC")
    db.expire_all()
    row = db.get(Activity, "1")
    assert row.timezone == "America/New_York" and row.kind == "running"


def test_activity_versions_order_across_probe_and_live_keys(db, tmp_path):
    from datetime import timedelta

    archive = LocalArchive(tmp_path)
    now = datetime.now(UTC)
    original = {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100}
    newer = ingest(
        db, archive, "activity", "1", {**original, "duration": 200}, "UTC", fetched_at=now
    )
    ingest(
        db, archive, "activities", "probe", [original], "UTC", fetched_at=now - timedelta(days=1)
    )
    db.expire_all()
    assert db.get(Activity, "1").duration_seconds == 200
    from uuid import UUID

    assert db.get(SourcePayload, UUID(newer["source_ref"])).fetched_at == now


def test_stale_fit_cannot_replace_newer_archive(db, tmp_path):
    from datetime import timedelta

    from garmin_ai.fit import store_fit

    archive = LocalArchive(tmp_path)
    now = datetime.now(UTC)
    ingest(
        db,
        archive,
        "activity",
        "1",
        {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100},
        "UTC",
    )
    store_fit(db, archive, "1", b"new corrupt fit", fetched_at=now)
    key = db.get(Activity, "1").fit_key
    result = store_fit(db, archive, "1", b"old corrupt fit", fetched_at=now - timedelta(hours=1))
    assert result["status"] == "stale" and db.get(Activity, "1").fit_key == key


@pytest.mark.parametrize(
    "endpoint",
    [
        "activity_details",
        "activity_splits",
        "activity_typed_splits",
        "activity_zones",
        "activity_weather",
    ],
)
def test_activity_parts_are_written_and_replaced(db, tmp_path, endpoint):
    from garmin_ai.models import ActivityPart

    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "activity",
        "1",
        {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100},
        "UTC",
    )
    assert (
        ingest(db, archive, endpoint, "1", [{"synthetic": 1}, {"synthetic": 2}], "UTC")["status"]
        == "archived"
    )
    assert db.scalar(select(func.count()).select_from(ActivityPart)) == 2
    ingest(db, archive, endpoint, "1", [{"synthetic": 3}], "UTC")
    assert db.scalar(select(func.count()).select_from(ActivityPart)) == 1


def test_shared_health_field_keeps_newest_endpoint_value(db, tmp_path):
    from datetime import timedelta

    archive = LocalArchive(tmp_path)
    now = datetime.now(UTC)
    ingest(db, archive, "daily", "2026-09-07", {"restingHeartRate": 60}, "UTC", fetched_at=now)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-07",
        {"restingHeartRate": 50},
        "UTC",
        fetched_at=now - timedelta(hours=1),
    )
    db.expire_all()
    assert db.scalar(select(HealthDay)).resting_hr == 60


def test_identical_new_observation_revalidates_shared_target(db, tmp_path):
    from datetime import timedelta

    archive = LocalArchive(tmp_path)
    now = datetime.now(UTC)
    base = {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100}
    ingest(db, archive, "activity", "1", {**base, "duration": 200}, "UTC", fetched_at=now)
    ingest(db, archive, "activities", "probe", [base], "UTC", fetched_at=now - timedelta(hours=1))
    ingest(db, archive, "activities", "probe", [base], "UTC", fetched_at=now + timedelta(hours=1))
    db.expire_all()
    assert db.get(Activity, "1").duration_seconds == 100


def test_empty_fit_advances_order_without_erasing_history(db, tmp_path):
    from datetime import timedelta

    from garmin_ai.fit import store_fit

    archive = LocalArchive(tmp_path)
    now = datetime.now(UTC)
    ingest(
        db,
        archive,
        "activity",
        "1",
        {"activityId": 1, "startTimeGMT": "2026-09-07T10:00:00Z", "duration": 100},
        "UTC",
    )
    assert store_fit(db, archive, "1", b"", fetched_at=now)["status"] == "empty"
    assert (
        store_fit(db, archive, "1", b"older", fetched_at=now - timedelta(hours=1))["status"]
        == "stale"
    )
    assert db.get(Activity, "1").fit_key is None
