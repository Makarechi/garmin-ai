from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from garmin_ai.models import AppState, SourcePayload, TimelineInterval
from garmin_ai.normalize import PARSER_VERSION
from garmin_ai.replay import canonical_source, projection_mismatch

NOW = datetime(2026, 9, 10, tzinfo=UTC)


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
    if kind == "timeline":
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
    ) == (kind not in {"unrelated", "missing"})


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
    statement = select(SourcePayload.id).where(projection_mismatch())
    assert list(db.scalars(statement)) == []
    sql = str(statement.compile(db.bind, compile_kwargs={"literal_binds": True}))
    plan = db.scalar(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql))[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    journal_scans = [
        node
        for node in nodes(plan)
        if node.get("Relation Name") == "app_state"
        and any(
            prefix in str(node.get("Filter", ""))
            for prefix in (
                "sample-owner:",
                "activity-version:",
            )
        )
    ]
    assert journal_scans
    assert all(node["Actual Loops"] <= 1 for node in journal_scans)
