from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from garmin_ai.models import Activity, AppState, HealthDay, SourcePayload, TimelineInterval
from garmin_ai.normalize import PARSER_VERSION
from garmin_ai.replay import canonical_source, projection_mismatch

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


def revisions(db, count=1):
    current = SourcePayload(
        endpoint="readiness",
        source_key="synthetic",
        payload_hash="current",
        payload={},
        archive_key="synthetic/current",
        fetched_at=NOW,
        parser_version=PARSER_VERSION,
        status="normalized",
    )
    old = [
        SourcePayload(
            endpoint="readiness",
            source_key="synthetic",
            payload_hash=f"old-{i}",
            payload={},
            archive_key=f"synthetic/old-{i}",
            fetched_at=NOW - timedelta(days=1),
            parser_version=0,
            status="archived",
        )
        for i in range(count)
    ]
    db.add_all([current, *old])
    db.flush()
    db.add(
        AppState(
            key="ingest:garmin_connect:readiness:synthetic",
            value={
                "source_ref": str(current.id),
                "requested_at": NOW.isoformat(),
            },
        )
    )
    db.flush()
    return old


@pytest.mark.parametrize(
    "shape",
    [
        "scalar",
        "array",
        "nested_array",
        "root_array",
        "nested_object",
        "null",
        "empty",
        "other",
        "mixed",
        "root_scalar",
    ],
)
def test_activity_ownership_preserves_existing_json_path_rules(db, shape):
    old = revisions(db)[0]
    ref = str(old.id)
    owners = {
        "scalar": {"field": ref},
        "array": {"field": [ref]},
        "nested_array": {"field": [[ref]]},
        "root_array": [{"field": ref}],
        "nested_object": {"field": {"nested": ref}},
        "null": {"field": None},
        "empty": {},
        "other": {"field": str(uuid4())},
        "mixed": {"field": [None, 42, ref]},
        "root_scalar": ref,
    }[shape]
    db.add(AppState(key="activity-version:synthetic", value={"owners": owners}))
    # A null reference in an unrelated journal must not poison membership.
    db.add(AppState(key="sample-owner:unrelated", value={"source_ref": None}))
    db.flush()
    expected = db.scalar(
        select(
            func.jsonb_path_exists(
                AppState.value["owners"],
                text("'$.* ? (@ == $ref)'::jsonpath"),
                func.jsonb_build_object("ref", ref),
            )
        ).where(AppState.key == "activity-version:synthetic")
    )
    assert (
        bool(
            db.scalar(
                select(SourcePayload.id).where(
                    SourcePayload.id == old.id,
                    canonical_source(),
                )
            )
        )
        == expected
    )


@pytest.mark.parametrize(
    "kind",
    [
        "sample-owner",
        "interval-owner",
        "observation-owner",
        "activity-parts-owner",
        "timeline",
        "unrelated",
        "missing",
        "health_field",
        "health_time",
        "health_unknown",
    ],
)
def test_retained_journals_ignore_unrelated_and_null_references(db, kind):
    old = revisions(db)[0]
    db.add(AppState(key="sample-owner:null", value={"source_ref": None}))
    db.add(
        TimelineInterval(
            id="null",
            start=NOW,
            end=NOW + timedelta(hours=1),
            label="synthetic",
            source="synthetic",
            confidence=1,
            evidence={"source_ref": None},
        )
    )
    if kind.startswith("health_"):
        key = {
            "health_field": "field:steps",
            "health_time": "time:steps",
            "health_unknown": "field:synthetic_unknown",
        }[kind]
        db.add(HealthDay(day=NOW.date(), sources={key: str(old.id), "field:resting_hr": None}))
    elif kind == "timeline":
        db.add(
            TimelineInterval(
                id="owned",
                start=NOW,
                end=NOW + timedelta(hours=1),
                label="synthetic",
                source="synthetic",
                confidence=1,
                evidence={"source_ref": str(old.id)},
            )
        )
    elif kind != "missing":
        db.add(AppState(key=kind + ":synthetic", value={"source_ref": str(old.id)}))
    db.flush()
    assert bool(
        db.scalar(
            select(SourcePayload.id).where(
                SourcePayload.id == old.id,
                canonical_source(),
            )
        )
    ) == (kind not in {"unrelated", "missing", "health_time", "health_unknown"})


def test_owner_sets_compose_in_repeated_predicates(db):
    revisions(db)
    for _ in range(3):
        assert (
            len(
                list(
                    db.scalars(
                        select(SourcePayload.id).where(
                            canonical_source() | ~canonical_source(),
                        )
                    )
                )
            )
            == 2
        )


def test_owner_sets_read_fresh_references_for_each_query(db):
    old = revisions(db)[0]
    state = AppState(key="sample-owner:changing", value={})
    db.add(state)
    for reference, expected in (
        (None, False),
        (str(old.id), True),
        (str(uuid4()), False),
        (None, False),
    ):
        state.value = {"source_ref": reference}
        db.flush()
        assert (
            bool(
                db.scalar(
                    select(SourcePayload.id).where(
                        SourcePayload.id == old.id,
                        canonical_source(),
                    )
                )
            )
            == expected
        )


def test_owner_journals_are_not_rescanned_for_every_historical_revision(db):
    revisions(db, count=250)
    for i in range(1000):
        db.add(AppState(key=f"sample-owner:synthetic-{i}", value={"source_ref": str(uuid4())}))
        db.add(
            AppState(
                key=f"activity-version:synthetic-{i}",
                value={
                    "owners": {"field": str(uuid4())},
                },
            )
        )
    db.flush()
    db.execute(text("ANALYZE source_payloads"))
    db.execute(text("ANALYZE app_state"))
    db.execute(text("SET LOCAL jit = off"))
    db.execute(text("SET LOCAL work_mem = '2MB'"))
    statement = select(SourcePayload.id).where(projection_mismatch())
    assert list(db.scalars(statement)) == []
    sql = str(statement.compile(db.bind, compile_kwargs={"literal_binds": True}))
    plan = db.scalar(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql))[0]["Plan"]

    journal_scans = [
        node
        for node in plan_nodes(plan)
        if (
            node.get("Relation Name") == "app_state"
            and any(
                prefix in str(node.get("Filter", ""))
                for prefix in ("sample-owner:", "activity-version:")
            )
        )
        or node.get("Relation Name")
        in {"measurements", "metric_observations", "timeline_intervals", "health_days"}
    ]
    assert journal_scans
    assert all(node["Actual Loops"] <= 1 for node in journal_scans)


def test_failed_fit_siblings_are_checked_once_per_stream(db):
    count = 60
    current = SourcePayload(
        endpoint="activity_fit",
        source_key="synthetic",
        payload_hash="current-fit",
        payload=None,
        archive_key="synthetic/current-fit",
        fetched_at=NOW,
        parser_version=PARSER_VERSION,
        status="normalized",
    )
    db.add(current)
    db.add(
        Activity(
            id="synthetic",
            kind="synthetic",
            start=NOW,
            end=NOW + timedelta(seconds=1),
            timezone="UTC",
            fit_key=current.archive_key,
            details={"fit_status": "normalized", "parsed_fit_key": current.archive_key},
        )
    )
    db.flush()
    db.add(
        AppState(
            key="fit-version:synthetic",
            value={
                "source_ref": str(current.id),
                "requested_at": NOW.isoformat(),
            },
        )
    )
    for index in range(count):
        failed = SourcePayload(
            endpoint="activity_fit",
            source_key="synthetic",
            payload_hash=f"failed-fit-{index}",
            payload=None,
            archive_key=f"synthetic/failed-fit-{index}",
            fetched_at=NOW + timedelta(seconds=index + 1),
            parser_version=PARSER_VERSION - 1,
            status="error",
        )
        db.add(failed)
        db.flush()
        db.add(
            AppState(
                key=f"ingest-meta:{failed.id}",
                value={
                    "failed_parser_version": PARSER_VERSION,
                },
            )
        )
    db.flush()
    db.execute(text("ANALYZE source_payloads"))
    db.execute(text("ANALYZE app_state"))
    db.execute(text("SET LOCAL jit = off"))
    db.execute(text("SET LOCAL work_mem = '2MB'"))
    statement = select(SourcePayload.id).where(projection_mismatch())
    assert list(db.scalars(statement)) == []
    sql = str(statement.compile(db.bind, compile_kwargs={"literal_binds": True}))
    plan = db.scalar(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql))[0]["Plan"]
    failure_metadata = [
        node
        for node in plan_nodes(plan)
        if node.get("Relation Name") == "app_state"
        and (
            "ingest-meta:" in str(node.get("Index Cond", ""))
            or "failed_parser_version" in str(node.get("Filter", ""))
        )
    ]
    assert failure_metadata
    assert sum(node["Actual Loops"] for node in failure_metadata) <= count * 2
    sibling_visits = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in plan_nodes(plan)
        if node.get("Relation Name") == "source_payloads"
    )
    assert sibling_visits <= count * 4
