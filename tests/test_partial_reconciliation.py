from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, Insight, Measurement
from garmin_ai.reconciliation import Replacement

START = datetime(2026, 9, 10, tzinfo=UTC)


def test_summary_only_body_battery_endpoint_rejects_sample_replacement():
    with pytest.raises(ValueError, match="channels"):
        Replacement(START, START + timedelta(hours=1), ("body_battery",), "synthetic").validate(
            "body_battery"
        )


def test_hrv_authoritative_interval_removes_omitted_samples(db, tmp_path):
    archive = LocalArchive(tmp_path)
    payload = {
        "hrvReadings": [
            {
                "readingTimeGMT": int((START + timedelta(minutes=i)).timestamp() * 1000),
                "hrvValue": 40,
            }
            for i in range(2)
        ]
    }
    ingest(db, archive, "hrv", "2026-09-10", payload, "UTC", fetched_at=START)
    ingest(
        db,
        archive,
        "hrv",
        "2026-09-10",
        {},
        "UTC",
        fetched_at=START + timedelta(hours=1),
        replacement=Replacement(
            START, START + timedelta(minutes=1), ("hrv_rmssd_ms",), "synthetic"
        ),
    )
    rows = db.scalars(select(Measurement)).all()
    assert len(rows) == 1 and rows[0].ts == START + timedelta(minutes=1)


def test_alternate_source_does_not_overwrite_or_clear_garmin_samples(db, tmp_path):
    archive = LocalArchive(tmp_path)
    for source, value in [("garmin_connect", 70), ("synthetic_import", 90)]:
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(2, value),
            "UTC",
            source=source,
            fetched_at=START,
        )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 4
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        {},
        "UTC",
        source="synthetic_import",
        fetched_at=START + timedelta(hours=1),
        replacement=Replacement(
            START, START + timedelta(minutes=1), ("heart_rate_bpm",), "synthetic"
        ),
    )
    rows = db.scalars(select(Measurement)).all()
    assert len(rows) == 3
    assert len([r for r in rows if r.source == "garmin_connect" and r.value == 70]) == 2


def test_metric_order_and_duplicates_do_not_reapply_identical_contract(db, tmp_path):
    archive = LocalArchive(tmp_path)
    results = []
    for metrics in [
        ("stress_score", "body_battery"),
        ("body_battery", "stress_score", "stress_score"),
    ]:
        results.append(
            ingest(
                db,
                archive,
                "stress",
                "2026-09-10",
                {},
                "UTC",
                fetched_at=START,
                replacement=Replacement(START, START + timedelta(hours=1), metrics, "synthetic"),
            )
        )
    assert [r["status"] for r in results] == ["empty", "unchanged"]


def points(count, value=70):
    return {
        "heartRateValues": [
            [int((START + timedelta(minutes=minute)).timestamp() * 1000), value]
            for minute in range(count)
        ]
    }


def test_ten_point_partial_response_does_not_erase_seven_hundred_observations(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(700),
        "UTC",
        fetched_at=START + timedelta(hours=12),
    )
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(10, 80),
        "UTC",
        fetched_at=START + timedelta(hours=13),
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 700
    assert db.scalar(select(Measurement.value).where(Measurement.ts == START)) == 80
    assert (
        db.scalar(select(Measurement.value).where(Measurement.ts == START + timedelta(minutes=699)))
        == 70
    )
    assert (
        db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10").value["completeness"]
        == "unverified"
    )


def test_attested_replacement_only_removes_covered_interval_and_invalidates_insights(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(20),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    db.add(
        Insight(
            category="synthetic",
            statement="synthetic",
            evidence={},
            sample_size=1,
            status="accepted",
            dedup_key="old",
        )
    )
    replacement = Replacement(
        START,
        START + timedelta(minutes=10),
        ("heart_rate_bpm",),
        "synthetic-authoritative-adapter:v1",
    )
    for _ in range(2):
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(1, 90),
            "UTC",
            fetched_at=START + timedelta(hours=2),
            replacement=replacement,
        )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 11
    assert db.scalar(select(Measurement.value).where(Measurement.ts == START)) == 90
    assert (
        db.scalar(select(Measurement.value).where(Measurement.ts == START + timedelta(minutes=10)))
        == 70
    )
    assert db.scalar(select(Insight)).status == "superseded"
    state = db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10", populate_existing=True)
    assert state.value["replacement"] == replacement.serialize()
    stale = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(20),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    assert stale["status"] == "stale"
    assert db.scalar(select(func.count()).select_from(Measurement)) == 11


def test_attested_empty_snapshot_can_clear_only_its_channel(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(2),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    replacement = Replacement(
        START,
        START + timedelta(minutes=1),
        ("heart_rate_bpm",),
        "synthetic-authoritative-adapter:v1",
    )
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        {},
        "UTC",
        fetched_at=START + timedelta(hours=2),
        replacement=replacement,
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 1


@pytest.mark.parametrize(
    "metrics,evidence", [(("stress_score",), "synthetic"), (("heart_rate_bpm",), "")]
)
def test_replacement_requires_channel_contract_and_evidence(db, tmp_path, metrics, evidence):
    with pytest.raises(ValueError):
        ingest(
            db,
            LocalArchive(tmp_path),
            "heart_rate",
            "2026-09-10",
            {},
            "UTC",
            replacement=Replacement(START, START + timedelta(hours=1), metrics, evidence),
        )


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("live", [False, True])
def test_parser_replay_rebuilds_current_revision_atomically(db, tmp_path, monkeypatch, fail, live):
    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", "2026-09-10", points(3), "UTC", fetched_at=START)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    current = db.scalar(
        select(SourcePayload).where(
            SourcePayload.payload_hash
            == db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10").value["hash"]
        )
    )
    current.parser_version = PARSER_VERSION - 1
    db.add(
        Measurement(
            ts=START,
            metric="obsolete_parser_metric",
            local_date=START.date(),
            source="garmin_connect",
            source_ref=current.id,
            value=1,
            unit="synthetic",
            quality="valid",
        )
    )
    db.flush()
    if fail:

        def broken(*args):
            raise ValueError("synthetic parser failure")

        monkeypatch.setattr("garmin_ai.ingest.normalize", broken)
    result = (
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(1, 80),
            "UTC",
            fetched_at=START + timedelta(hours=2),
        )
        if live
        else replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": str(current.id), "target_version": PARSER_VERSION},
        )
    )
    obsolete = db.scalar(select(Measurement).where(Measurement.metric == "obsolete_parser_metric"))
    assert (obsolete is not None) == fail
    assert result["status"] == ("error" if fail else "normalized")
    readings = db.scalars(
        select(Measurement).where(Measurement.metric == "heart_rate_bpm").order_by(Measurement.ts)
    ).all()
    assert [row.value for row in readings] == [80, 70, 70]


def test_replacement_order_uses_absolute_instants_during_dst_fold():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Bratislava")
    first = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    second = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0)
    with pytest.raises(ValueError, match="positive"):
        Replacement(first, second, ("heart_rate_bpm",), "synthetic").validate("heart_rate")
    valid = Replacement(second, first, ("heart_rate_bpm",), "synthetic")
    valid.validate("heart_rate")
    encoded = valid.serialize()
    assert datetime.fromisoformat(encoded["end"]) - datetime.fromisoformat(
        encoded["start"]
    ) == timedelta(minutes=30)


@pytest.mark.parametrize("tied", [False, True])
def test_failed_authoritative_contract_survives_replay(db, tmp_path, monkeypatch, tied):
    from importlib import import_module

    from garmin_ai.config import Settings
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_source

    module = import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    first = ingest(db, archive, "heart_rate", "2026-09-10", points(2), "UTC", fetched_at=START)
    contract = Replacement(START, START + timedelta(minutes=1), ("heart_rate_bpm",), "synthetic")
    original = module.normalize

    def fail(*args, **kwargs):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(module, "normalize", fail)
    failed = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        {},
        "UTC",
        fetched_at=START if tied else START + timedelta(hours=1),
        replacement=contract,
    )
    assert failed["status"] == "error"
    assert db.scalar(select(func.count()).select_from(Measurement)) == 2
    monkeypatch.setattr(module, "normalize", original)
    if tied:
        from uuid import UUID

        from garmin_ai.models import SourcePayload

        db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
        db.flush()
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"target_version": PARSER_VERSION, "raw_ref": first["source_ref"]},
        )
    state = db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10", populate_existing=True)
    assert state.value["latest_attempt"]["replacement"] == contract.serialize()
    metadata = db.get(AppState, "ingest-meta:" + failed["source_ref"], populate_existing=True)
    metadata.value = {**metadata.value, "failed_parser_version": PARSER_VERSION - 1}
    db.flush()
    replay_source(
        db,
        archive,
        Settings(timezone="UTC"),
        {"target_version": PARSER_VERSION, "raw_ref": failed["source_ref"]},
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 1


def test_live_correction_cancels_only_pending_context_questions(db, tmp_path):
    from garmin_ai.models import PendingQuestion

    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", "2026-09-10", points(2), "UTC", fetched_at=START)
    rows = []
    for kind, status in [("context", "pending"), ("context", "sent"), ("migraine", "pending")]:
        row = PendingQuestion(
            kind=kind,
            status=status,
            text="synthetic",
            evidence={},
            priority=1,
            earliest_send_at=START,
            expires_at=START + timedelta(days=1),
            dedup_key=kind + status,
        )
        rows.append(row)
        db.add(row)
    db.flush()
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(3),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    assert [row.status for row in rows] == ["cancelled", "sent", "pending"]


@pytest.mark.parametrize("endpoint", ["respiration", "spo2", "heart_rate"])
def test_unrelated_or_still_supported_context_questions_remain_pending(
    db, tmp_path, monkeypatch, endpoint
):
    from garmin_ai import proactive
    from garmin_ai.models import PendingQuestion

    question = PendingQuestion(
        kind="context",
        status="pending",
        text="synthetic",
        evidence={"start": START.isoformat(), "end": (START + timedelta(minutes=30)).isoformat()},
        priority=1,
        earliest_send_at=START,
        expires_at=START + timedelta(days=1),
        dedup_key="synthetic-context",
    )
    db.add(question)
    db.flush()
    calls = []
    monkeypatch.setattr(
        proactive, "context_physiology", lambda *args: calls.append(args) or {"hr_samples": 7}
    )
    payload = points(3) if endpoint == "heart_rate" else {"synthetic": True}
    ingest(db, LocalArchive(tmp_path), endpoint, "2026-09-10", payload, "UTC", fetched_at=START)
    assert question.status == "pending"
    assert bool(calls) == (endpoint == "heart_rate")
    if endpoint == "heart_rate":
        assert question.evidence["hr_samples"] == 7


@pytest.mark.parametrize("attested", [False, True])
def test_rebuild_preserves_overwritten_partial_unless_authoritatively_deleted(
    db, tmp_path, monkeypatch, attested
):
    from garmin_ai import ingest as ingest_module
    from garmin_ai import projection_history
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION

    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", "2026-09-10", points(1, 70), "UTC", fetched_at=START)
    if attested:
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            {},
            "UTC",
            fetched_at=START + timedelta(minutes=1),
            replacement=Replacement(
                START, START + timedelta(minutes=1), ("heart_rate_bpm",), "synthetic-complete"
            ),
        )
    latest = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=2),
    )
    from uuid import UUID

    current = db.get(SourcePayload, UUID(latest["source_ref"]))
    current.parser_version = PARSER_VERSION - 1
    normalizer = ingest_module.normalize

    def changed_parser(session, endpoint, key, payload, ref, timezone):
        if str(ref) == latest["source_ref"]:
            return "empty"
        return normalizer(session, endpoint, key, payload, ref, timezone)

    monkeypatch.setattr(ingest_module, "normalize", changed_parser)
    monkeypatch.setattr(projection_history, "normalize", changed_parser)
    result = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=2),
        rebuild_projection=True,
    )
    assert result["status"] == "empty"
    rows = db.scalars(select(Measurement)).all()
    if attested:
        assert rows == []
    else:
        assert (
            len(rows) == 1
            and rows[0].value == 70
            and str(rows[0].source_ref) == latest["source_ref"]
        )
        # A later parser rejects both revisions: the restored fallback must be rechecked.
        current.parser_version = PARSER_VERSION - 1
        monkeypatch.setattr(ingest_module, "normalize", lambda *args: "empty")
        monkeypatch.setattr(projection_history, "normalize", lambda *args: "empty")
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(1, 80),
            "UTC",
            fetched_at=START + timedelta(minutes=2),
            rebuild_projection=True,
        )
        assert db.scalars(select(Measurement)).all() == []


def test_missing_previous_archive_aborts_rebuild_without_losing_measurements(db, tmp_path):
    from uuid import UUID

    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION

    archive = LocalArchive(tmp_path)
    previous = ingest(
        db, archive, "heart_rate", "2026-09-10", points(2, 70), "UTC", fetched_at=START
    )
    latest = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=2),
    )
    db.get(SourcePayload, UUID(previous["source_ref"])).archive_key = "missing-synthetic.json"
    db.get(SourcePayload, UUID(latest["source_ref"])).parser_version = PARSER_VERSION - 1
    result = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=2),
        rebuild_projection=True,
    )
    assert result["status"] == "error"
    assert list(db.scalars(select(Measurement.value).order_by(Measurement.ts))) == [80, 70]


def test_legacy_repeated_application_order_is_not_invented(db, tmp_path):
    from uuid import UUID

    from sqlalchemy import delete

    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.projection_history import history_key

    archive = LocalArchive(tmp_path)
    for index, value in enumerate((70, 80, 70, 90)):
        latest = ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(1, value),
            "UTC",
            fetched_at=START + timedelta(minutes=index),
        )
    current = db.get(SourcePayload, UUID(latest["source_ref"]))
    db.execute(delete(AppState).where(AppState.key == history_key(current)))
    current.parser_version = PARSER_VERSION - 1
    result = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 90),
        "UTC",
        fetched_at=START + timedelta(minutes=3),
        rebuild_projection=True,
    )
    assert result["status"] == "error"
    assert list(db.scalars(select(Measurement.value))) == [90]
    # New source observations remain usable, but retain the unknown legacy boundary.
    result = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 95),
        "UTC",
        fetched_at=START + timedelta(minutes=4),
    )
    assert result["status"] == "normalized"
    history = db.get(AppState, history_key(current), populate_existing=True).value["applications"]
    assert history[0] == {"legacy_order_unknown": True}


def test_physiological_questions_use_one_source_without_interleaving(db):
    from garmin_ai.proactive import context_physiology, elevated_stress_runs, personal_hr_threshold

    now = START + timedelta(hours=12)
    left, right = now - timedelta(minutes=22), now
    for source, baseline, stress, hr in [
        ("garmin_connect", 70, 90, 100),
        ("synthetic_import", 200, 0, 0),
    ]:
        for day in range(2, 9):
            for minute in range(30):
                db.add(
                    Measurement(
                        ts=now - timedelta(days=day, minutes=minute),
                        local_date=(now - timedelta(days=day, minutes=minute)).date(),
                        metric="heart_rate_bpm",
                        value=baseline,
                        unit="bpm",
                        source=source,
                        quality="observed",
                    )
                )
        for minute in range(0, 22, 2):
            instant = left + timedelta(minutes=minute)
            for metric, value, unit in [
                ("stress_score", stress, "score"),
                ("heart_rate_bpm", hr, "bpm"),
            ]:
                db.add(
                    Measurement(
                        ts=instant,
                        local_date=instant.date(),
                        metric=metric,
                        value=value,
                        unit=unit,
                        source=source,
                        quality="observed",
                    )
                )
    db.flush()
    assert personal_hr_threshold(db, "UTC", now) == 70
    assert len(elevated_stress_runs(db, left, right)) == 1
    result = context_physiology(db, "UTC", now, left, right)
    assert result and result["hr_samples"] == 11 and result["baseline_hr_p95"] == 70


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("devices", [{"synthetic": True}]),
        ("all_day_events", {"synthetic": True}),
        ("heart_rate", {}),
        ("stress", {"stressValuesArray": []}),
        ("heart_rate", {"heartRateValues": []}),
    ],
)
def test_non_projecting_fetch_does_not_supersede_insights(db, tmp_path, endpoint, payload):
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-preserved",
    )
    db.add(insight)
    db.flush()
    result = ingest(
        db, LocalArchive(tmp_path), endpoint, "2026-09-10", payload, "UTC", fetched_at=START
    )
    assert result["status"] in {"archived", "empty", "normalized"}
    db.refresh(insight)
    assert insight.status == "accepted"


def test_empty_retry_after_failed_reparse_invalidates_retained_projection(
    db, tmp_path, monkeypatch
):
    import importlib
    from uuid import UUID

    from garmin_ai.models import PendingQuestion, SourcePayload
    from garmin_ai.normalize import PARSER_VERSION

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    payload = points(1, 80)
    result = ingest(db, archive, "heart_rate", str(START.date()), payload, "UTC", fetched_at=START)
    raw = db.get(SourcePayload, UUID(result["source_ref"]))
    raw.parser_version = PARSER_VERSION - 1
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-retry",
    )
    question = PendingQuestion(
        kind="context",
        status="pending",
        text="synthetic",
        evidence={},
        priority=1,
        earliest_send_at=START,
        expires_at=START + timedelta(days=1),
        dedup_key="synthetic-retry-question",
    )
    db.add_all([insight, question])
    db.flush()

    def fail(*args):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(module, "normalize", fail)
    assert (
        ingest(db, archive, "heart_rate", str(START.date()), payload, "UTC", fetched_at=START)[
            "status"
        ]
        == "error"
    )
    assert db.scalar(select(Measurement)) is not None
    monkeypatch.setattr(module, "normalize", lambda *args: "empty")
    monkeypatch.setattr("garmin_ai.projection_history.normalize", lambda *args: "empty")
    assert (
        ingest(db, archive, "heart_rate", str(START.date()), payload, "UTC", fetched_at=START)[
            "status"
        ]
        == "empty"
    )
    assert db.scalar(select(Measurement)) is None
    db.refresh(insight)
    db.refresh(question)
    assert insight.status == "superseded"
    assert question.status == "cancelled"


@pytest.mark.parametrize("attested", [False, True])
@pytest.mark.parametrize("same_time", [False, True])
def test_retained_replay_preserves_current_samples_and_contract(db, tmp_path, attested, same_time):
    from uuid import UUID

    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.projection_history import load_history
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(3, 70), "UTC", fetched_at=START
    )
    latest = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(2, 90),
        "UTC",
        fetched_at=START if same_time else START + timedelta(minutes=5),
        replacement=Replacement(
            START, START + timedelta(minutes=2), ("heart_rate_bpm",), "synthetic"
        )
        if attested
        else None,
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    history = load_history(db, row)
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {
                "raw_ref": first["source_ref"],
                "target_version": PARSER_VERSION,
            },
        )["status"]
        == "normalized"
    )
    assert list(db.scalars(select(Measurement.value).order_by(Measurement.ts))) == [90, 90, 70]
    assert db.get(SourcePayload, UUID(latest["source_ref"])).parser_version == PARSER_VERSION
    assert load_history(db, row) == history
    assert (
        db.get(AppState, "ingest:garmin_connect:heart_rate:" + str(START.date())).value[
            "source_ref"
        ]
        == latest["source_ref"]
    )


def test_failed_legacy_owner_preserves_unknown_history_boundary(db, tmp_path):
    from uuid import UUID

    from garmin_ai.models import SourcePayload
    from garmin_ai.projection_history import history_key, load_history

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(2, 70), "UTC", fetched_at=START
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    db.delete(db.get(AppState, history_key(row)))
    row.status = "error"
    db.flush()
    ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(1, 90),
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    assert load_history(db, row)[0] == {"legacy_order_unknown": True}


@pytest.mark.parametrize("empty", [{}, {"heartRateValues": []}])
def test_legacy_empty_response_does_not_block_retained_owner_replay(db, tmp_path, empty):
    from uuid import UUID

    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.projection_history import history_key
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(2, 70), "UTC", fetched_at=START
    )
    ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        empty,
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    db.delete(db.get(AppState, history_key(row)))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert list(db.scalars(select(Measurement.value))) == [70, 70]


def test_repeated_application_with_identical_timestamp_is_journaled(db, tmp_path):
    from uuid import UUID

    from garmin_ai.models import SourcePayload
    from garmin_ai.projection_history import load_history, previous_observations

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(2, 70), "UTC", fetched_at=START
    )
    second = ingest(
        db, archive, "heart_rate", str(START.date()), points(2, 90), "UTC", fetched_at=START
    )
    ingest(db, archive, "heart_rate", str(START.date()), points(2, 70), "UTC", fetched_at=START)
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    history = load_history(db, row)
    assert [item["raw_ref"] for item in history] == [
        first["source_ref"],
        second["source_ref"],
        first["source_ref"],
    ]
    assert {item["value"] for item in previous_observations(db, archive, row, history)} == {70}


@pytest.mark.parametrize("intervening", [False, True])
def test_identical_shared_daily_response_invalidates_changed_projection(db, tmp_path, intervening):
    archive = LocalArchive(tmp_path)
    payload = {"restingHeartRate": 60}
    ingest(db, archive, "daily", str(START.date()), payload, "UTC", fetched_at=START)
    if intervening:
        ingest(
            db,
            archive,
            "heart_rate",
            str(START.date()),
            {"restingHeartRate": 70},
            "UTC",
            fetched_at=START + timedelta(minutes=1),
        )
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-shared-update",
    )
    db.add(insight)
    db.flush()
    ingest(
        db,
        archive,
        "daily",
        str(START.date()),
        payload,
        "UTC",
        fetched_at=START + timedelta(minutes=2),
    )
    db.refresh(insight)
    assert insight.status == ("superseded" if intervening else "accepted")


@pytest.mark.parametrize("reverse", [False, True])
def test_newly_accepted_sample_collision_uses_application_order(db, tmp_path, monkeypatch, reverse):
    from uuid import UUID

    import garmin_ai.normalize as module
    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value in (77, 88) else original(value, **kw)
    )
    refs = []
    ts = int(START.timestamp() * 1000)
    for i, value in enumerate((77, 88, 99)):
        payload = {"heartRateValues": [[ts + (i + 1) * 60000, 70]]}
        if i < 2:
            payload["heartRateValues"].append([ts, value])
        refs.append(
            ingest(
                db,
                archive,
                "heart_rate",
                str(START.date()),
                payload,
                "UTC",
                fetched_at=START + timedelta(minutes=i),
            )
        )
    for result in refs[:2]:
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    monkeypatch.setattr(module, "numeric", original)
    for result in reversed(refs[:2]) if reverse else refs[:2]:
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    assert db.get(Measurement, (START, "heart_rate_bpm", "garmin_connect")).value == 88


def test_invalid_only_legacy_stress_does_not_block_retained_replay(db, tmp_path):
    from uuid import UUID

    from sqlalchemy import delete

    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    ts = int(START.timestamp() * 1000)
    first = ingest(
        db,
        archive,
        "stress",
        str(START.date()),
        {"stressValuesArray": [[ts, 30]]},
        "UTC",
        fetched_at=START,
    )
    ingest(
        db,
        archive,
        "stress",
        str(START.date()),
        {"stressValuesArray": [[ts, -1]]},
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    db.execute(delete(AppState).where(AppState.key.startswith("ingest-history:")))
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert db.get(Measurement, (START, "stress_score", "garmin_connect")).value == 30


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("attested", [False, True])
def test_sample_provenance_only_refresh_preserves_insights(db, tmp_path, changed, attested):
    from uuid import UUID

    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", str(START.date()), points(1), "UTC", fetched_at=START)
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-provenance",
    )
    db.add(insight)
    db.flush()
    payload = {**points(1), "ignored": "new metadata"}
    contract = (
        Replacement(START, START + timedelta(minutes=1), ("heart_rate_bpm",), "synthetic")
        if attested
        else None
    )
    if changed:
        payload["heartRateValues"][0][1] += 1
    result = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        payload,
        "UTC",
        fetched_at=START + timedelta(minutes=1),
        replacement=contract,
    )
    db.refresh(insight)
    db.expire_all()
    assert insight.status == ("superseded" if changed else "accepted")
    assert db.get(Measurement, (START, "heart_rate_bpm", "garmin_connect")).source_ref == UUID(
        result["source_ref"]
    )


def test_same_raw_failed_reparse_uses_new_contract(db, tmp_path, monkeypatch):
    from importlib import import_module

    from garmin_ai.config import Settings
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_source

    module = import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", str(START.date()), points(2), "UTC", fetched_at=START)
    first = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(1),
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    original = module.normalize

    def fail(*args, **kwargs):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(module, "normalize", fail)
    contract = Replacement(START, START + timedelta(minutes=2), ("heart_rate_bpm",), "synthetic")
    failed = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(1),
        "UTC",
        fetched_at=START + timedelta(minutes=2),
        replacement=contract,
    )
    assert failed["source_ref"] == first["source_ref"]
    assert failed["status"] == "error"
    monkeypatch.setattr(module, "normalize", original)
    metadata = db.get(AppState, "ingest-meta:" + first["source_ref"], populate_existing=True)
    metadata.value = {**metadata.value, "failed_parser_version": PARSER_VERSION - 1}
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_readiness_observation_provenance_refresh(db, tmp_path, changed):
    archive = LocalArchive(tmp_path)
    payload = {"calendarDate": str(START.date()), "timestamp": START.isoformat(), "score": 70}
    ingest(db, archive, "readiness", str(START.date()), payload, "UTC", fetched_at=START)
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-observation",
    )
    db.add(insight)
    db.flush()
    ingest(
        db,
        archive,
        "readiness",
        str(START.date()),
        {**payload, "score": 71 if changed else 70, "ignored": True},
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    db.refresh(insight)
    assert insight.status == ("superseded" if changed else "accepted")


@pytest.mark.parametrize("changed", [False, True])
def test_sleep_interval_provenance_does_not_invalidate(db, tmp_path, changed):
    archive = LocalArchive(tmp_path)
    ts = int(START.timestamp() * 1000)
    payload = {
        "dailySleepDTO": {"sleepStartTimestampGMT": ts, "sleepEndTimestampGMT": ts + 3600000}
    }
    ingest(db, archive, "sleep", str(START.date()), payload, "UTC", fetched_at=START)
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-sleep",
    )
    db.add(insight)
    db.flush()
    payload = {
        "dailySleepDTO": {
            **payload["dailySleepDTO"],
            "sleepEndTimestampGMT": ts + (7200000 if changed else 3600000),
        },
        "ignored": True,
    }
    ingest(
        db,
        archive,
        "sleep",
        str(START.date()),
        payload,
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    db.refresh(insight)
    assert insight.status == ("superseded" if changed else "accepted")


def test_readiness_sequences_survive_provenance_refresh(db, tmp_path):
    archive = LocalArchive(tmp_path)
    payload = [{"timestamp": START.isoformat(), "score": score} for score in (60, 70)]
    ingest(db, archive, "readiness", str(START.date()), payload, "UTC", fetched_at=START)
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="synthetic-sequences",
    )
    db.add(insight)
    db.flush()
    ingest(
        db,
        archive,
        "readiness",
        str(START.date()),
        [{**row, "ignored": True} for row in payload],
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    db.refresh(insight)
    assert insight.status == "accepted"


def test_authoritative_replacement_deletes_legacy_source_label(db, tmp_path):
    from sqlalchemy import update

    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(2),
        "UTC",
        source="synthetic_adapter",
        fetched_at=START,
    )
    db.execute(
        update(Measurement)
        .where(Measurement.source == "synthetic_adapter")
        .values(source="garmin_connect")
    )
    db.flush()
    contract = Replacement(START, START + timedelta(minutes=2), ("heart_rate_bpm",), "synthetic")
    result = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        {},
        "UTC",
        source="synthetic_adapter",
        fetched_at=START + timedelta(minutes=1),
        replacement=contract,
    )
    assert result["status"] == "empty"
    assert db.scalar(select(func.count()).select_from(Measurement)) == 0


@pytest.mark.parametrize("covered_minutes", [1, 2])
def test_later_replacement_closes_only_covered_legacy_targets(
    db, tmp_path, monkeypatch, covered_minutes
):
    from importlib import import_module
    from uuid import UUID

    from sqlalchemy import delete

    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.projection_history import history_key
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    legacy = ingest(
        db, archive, "heart_rate", str(START.date()), points(2, 70), "UTC", fetched_at=START
    )
    db.execute(
        delete(AppState).where(
            AppState.key == history_key(db.get(SourcePayload, UUID(legacy["source_ref"])))
        )
    )
    contract = Replacement(
        START,
        START + timedelta(minutes=covered_minutes),
        ("heart_rate_bpm",),
        "synthetic verified interval",
    )
    current = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(2, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=3),
        replacement=contract,
    )
    row = db.get(SourcePayload, UUID(current["source_ref"]))
    assert db.get(AppState, history_key(row), populate_existing=True).value["applications"][0] == {
        "legacy_order_unknown": True
    }
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    module = import_module("garmin_ai.normalize")
    original = module.numeric
    monkeypatch.setattr(
        module,
        "numeric",
        lambda value, **kwargs: None if value == 80 else original(value, **kwargs),
    )
    result = replay_source(
        db,
        archive,
        Settings(timezone="UTC"),
        {"raw_ref": current["source_ref"], "target_version": PARSER_VERSION},
    )
    db.expire_all()
    assert result["status"] == ("normalized" if covered_minutes == 2 else "error")
    assert list(db.scalars(select(Measurement.value).order_by(Measurement.ts))) == (
        [] if covered_minutes == 2 else [80, 80]
    )
    assert db.get(SourcePayload, row.id).parser_version == (
        PARSER_VERSION if covered_minutes == 2 else PARSER_VERSION - 1
    )


@pytest.mark.parametrize("empty", [{}, {"heartRateValues": []}])
def test_unverified_empty_fetches_do_not_exhaust_application_history(
    db, tmp_path, monkeypatch, empty
):
    from uuid import UUID

    import garmin_ai.projection_history as history
    from garmin_ai.models import SourcePayload

    monkeypatch.setattr(history, "LIMIT", 3)
    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(1, 70), "UTC", fetched_at=START
    )
    for i in range(1, 6):
        result = ingest(
            db,
            archive,
            "heart_rate",
            str(START.date()),
            empty,
            "UTC",
            fetched_at=START + timedelta(minutes=i),
        )
        assert result["status"] in {"empty", "normalized", "unchanged"}
    recovered = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=6),
    )
    assert recovered["status"] == "normalized"
    assert (
        db.get(
            Measurement, (START, "heart_rate_bpm", "garmin_connect"), populate_existing=True
        ).value
        == 80
    )
    key = history.history_key(db.get(SourcePayload, UUID(first["source_ref"])))
    assert len(db.get(AppState, key, populate_existing=True).value["applications"]) == 2
    # Verified empty responses must retain their deletion contract in the journal.
    result = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        {},
        "UTC",
        fetched_at=START + timedelta(minutes=7),
        replacement=Replacement(
            START, START + timedelta(minutes=1), ("heart_rate_bpm",), "synthetic completeness"
        ),
    )
    assert result["status"] == "empty"
    entries = db.get(AppState, key, populate_existing=True).value["applications"]
    assert len(entries) == 3 and entries[-1]["replacement"]
    assert (
        db.get(Measurement, (START, "heart_rate_bpm", "garmin_connect"), populate_existing=True)
        is None
    )


@pytest.mark.parametrize("status", ["normalized", "error"])
def test_legacy_owned_projection_is_evidence_despite_rejected_shape(db, tmp_path, status):
    from uuid import UUID

    from sqlalchemy import delete

    from garmin_ai.models import SourcePayload
    from garmin_ai.projection_history import history_key, load_history

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(2, 70), "UTC", fetched_at=START
    )
    legacy = db.get(SourcePayload, UUID(first["source_ref"]))
    db.execute(delete(AppState).where(AppState.key == history_key(legacy)))
    legacy.payload = {"heartRateValues": [{"legacy_timestamp": "synthetic"}]}
    legacy.status = status
    db.flush()
    current = SourcePayload(
        endpoint="heart_rate",
        source_key=str(START.date()),
        payload={},
        archive_key="synthetic",
        payload_hash="synthetic",
        fetched_at=START + timedelta(minutes=1),
    )
    db.add(current)
    db.flush()
    assert load_history(db, current) == [{"legacy_order_unknown": True}]


def test_ordered_partial_application_resolves_legacy_target(db, tmp_path):
    from uuid import UUID

    from sqlalchemy import delete

    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.projection_history import history_key
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "heart_rate", str(START.date()), points(1, 70), "UTC", fetched_at=START
    )
    db.execute(
        delete(AppState).where(
            AppState.key == history_key(db.get(SourcePayload, UUID(first["source_ref"])))
        )
    )
    current = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    row = db.get(SourcePayload, UUID(current["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": current["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert db.scalar(select(Measurement.value)) == 80


def test_nonempty_rejected_application_is_reconstructed_after_upgrade(db, tmp_path, monkeypatch):
    from importlib import import_module
    from uuid import UUID

    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.projection_history import history_key
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", str(START.date()), points(1, 70), "UTC", fetched_at=START)
    rejected = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        {"heartRateValues": [{"ts": int(START.timestamp() * 1000), "value": 80}]},
        "UTC",
        fetched_at=START + timedelta(minutes=1),
    )
    current = ingest(
        db,
        archive,
        "heart_rate",
        str(START.date()),
        points(1, 90),
        "UTC",
        fetched_at=START + timedelta(minutes=2),
    )
    row = db.get(SourcePayload, UUID(current["source_ref"]))
    entries = db.get(AppState, history_key(row), populate_existing=True).value["applications"]
    assert [item["raw_ref"] for item in entries][1] == rejected["source_ref"]
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    module = import_module("garmin_ai.normalize")
    original = module._normalize

    def upgraded(session, endpoint, key, payload, ref, timezone):
        if endpoint == "heart_rate":
            payload = {
                **payload,
                "heartRateValues": [
                    [point["ts"], point["value"]] if isinstance(point, dict) else point
                    for point in payload.get("heartRateValues", [])
                    if isinstance(point, dict) or point[1] != 90
                ],
            }
        return original(session, endpoint, key, payload, ref, timezone)

    monkeypatch.setattr(module, "_normalize", upgraded)
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": current["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert db.scalar(select(Measurement.value)) == 80
