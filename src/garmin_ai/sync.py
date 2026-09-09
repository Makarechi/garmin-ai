"""Persistent schedules and incremental Garmin jobs."""

import json
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from garminconnect import Garmin
from sqlalchemy import func, select

from garmin_ai.db import transaction
from garmin_ai.fit import store_fit
from garmin_ai.garmin import ENDPOINTS
from garmin_ai.ingest import ingest
from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState, TimelineInterval
from garmin_ai.normalize import timestamp, upsert

FREQUENT = {"daily", "heart_rate", "stress", "body_battery", "readiness", "steps"}


def schedule_sync(session, settings, now: datetime):
    local = now.astimezone(ZoneInfo(settings.timezone))
    slot = int(now.timestamp()) // 900
    for endpoint in ENDPOINTS:
        if endpoint.name in FREQUENT:
            enqueue(
                session,
                "garmin_endpoint",
                {"endpoint": endpoint.name, "key": str(local.date())},
                f"frequent:{endpoint.name}:{slot}",
                now + timedelta(seconds=random.uniform(0, 60)),
            )
    enqueue(
        session,
        "garmin_activities",
        {"offset": 0, "since": str(local.date() - timedelta(days=14))},
        f"activities:{slot}",
        now,
    )
    sleeps = session.scalars(
        select(TimelineInterval)
        .where(TimelineInterval.label == "sleep", TimelineInterval.end > now - timedelta(days=14))
        .order_by(TimelineInterval.end.desc())
        .limit(14)
    ).all()
    wake_hours = [r.end.astimezone(ZoneInfo(settings.timezone)).hour for r in sleeps]
    wake = sorted(wake_hours)[len(wake_hours) // 2] if wake_hours else 8
    if (local.hour - wake) % 24 in {23, 0, 1, 2, 3}:
        for name in ("sleep", "hrv", "readiness"):
            enqueue(
                session,
                "garmin_endpoint",
                {"endpoint": name, "key": str(local.date())},
                f"morning:{name}:{slot}",
                now,
            )
    # Calendar-key dedup makes restart-safe schedules without in-memory cron state.
    if 2 <= local.hour < 5:
        days = 30 if local.weekday() == 0 else 7
        for offset in range(days):
            day = local.date() - timedelta(days=offset)
            for endpoint in ENDPOINTS:
                if endpoint.scope == "day":
                    enqueue(
                        session,
                        "garmin_endpoint",
                        {"endpoint": endpoint.name, "key": str(day)},
                        f"reconcile:{local.date()}:{endpoint.name}:{day}",
                        now + timedelta(seconds=offset * 30),
                    )
    for endpoint in ENDPOINTS:
        # First daily snapshot after the active day has developed; night reconciliation
        # still repairs completed days, and frequent/morning feeds remain independent.
        if endpoint.scope == "day" and local.hour < 18:
            continue
        if endpoint.scope in {"day", "global"}:
            enqueue(
                session,
                "garmin_endpoint",
                {
                    "endpoint": endpoint.name,
                    "key": str(local.date()) if endpoint.scope == "day" else "global",
                },
                f"daily:{endpoint.name}:{local.date()}",
                now + timedelta(seconds=random.uniform(0, 120)),
            )


def import_probe(engine, archive, settings, path: Path):
    report = json.loads(path.read_text())
    imported = 0

    def requested_at(row):
        if row.get("fetched_at"):
            return datetime.fromisoformat(row["fetched_at"])
        archive_path = (archive.root / row["archive_key"]).resolve()
        if not archive_path.is_relative_to(archive.root.resolve()):
            raise ValueError("Invalid archive key")
        return datetime.fromtimestamp(archive_path.stat().st_mtime, UTC)

    # Activity identities must exist before importing child documents.
    if report.get("activity_list_archive"):
        activity_request = next(
            (
                row
                for row in report["requests"]
                if row.get("archive_key") == report["activity_list_archive"]
                and row["endpoint"] == "activities"
            ),
            {"archive_key": report["activity_list_archive"]},
        )
        with transaction(engine) as session:
            result = ingest(
                session,
                archive,
                "activities",
                "probe",
                json.loads(archive.read(report["activity_list_archive"])),
                settings.timezone,
                fetched_at=requested_at(activity_request),
            )
        if result["status"] == "error":
            raise ValueError("Probe activity list could not be normalized")
    errors = []
    for row in report["requests"]:
        if row["status"] not in {"available", "empty"}:
            continue
        with transaction(engine) as session:
            if row["endpoint"] == "activity_fit":
                try:
                    with session.begin_nested():
                        result = store_fit(
                            session,
                            archive,
                            row["key"],
                            archive.read(row["archive_key"]),
                            fetched_at=requested_at(row),
                        )
                        if result["status"] == "error":
                            errors.append(
                                {"endpoint": row["endpoint"], "error_type": result["error_type"]}
                            )
                except Exception as exc:
                    errors.append({"endpoint": row["endpoint"], "error_type": type(exc).__name__})
            else:
                result = ingest(
                    session,
                    archive,
                    row["endpoint"],
                    row["key"],
                    json.loads(archive.read(row["archive_key"])),
                    settings.timezone,
                    fetched_at=requested_at(row),
                )
                if result["status"] == "error":
                    errors.append({"endpoint": row["endpoint"], "error_type": result["error_type"]})
            imported += 1
    return {"imported": imported, "errors": errors}


def record_endpoint_fetch(session, endpoint, key, requested_at, result):
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    state_key = f"freshness:{endpoint}:{key}"
    old = session.get(AppState, state_key, populate_existing=True)
    previous = old.value if old else {}
    old_time = previous.get("fetched_at") or previous.get("success_at")
    if old_time and datetime.fromisoformat(old_time) > requested_at:
        return
    success = result["status"] != "fetch_error"
    upsert(
        session,
        AppState,
        dict(
            key=state_key,
            value={
                "fetched_at": requested_at.isoformat(),
                "success_at": requested_at.isoformat() if success else previous.get("success_at"),
                "normalized_at": requested_at.isoformat()
                if result["status"] not in {"error", "fetch_error", "stale"}
                else previous.get("normalized_at"),
                "status": result["status"],
                "source_key": key,
                "source_ref": result.get("source_ref"),
            },
        ),
        ["key"],
    )


def run_garmin_job(engine, reader, archive, settings, kind, payload):
    now = datetime.now(UTC)
    if kind == "garmin_endpoint":
        endpoint = next(e for e in ENDPOINTS if e.name == payload["endpoint"])
        key = payload["key"]
        try:
            value = reader.fetch(
                endpoint,
                day=date.fromisoformat(key) if endpoint.scope == "day" else None,
                activity_id=key if endpoint.scope == "activity" else None,
            )
        except Exception:
            with transaction(engine) as session:
                record_endpoint_fetch(session, endpoint.name, key, now, {"status": "fetch_error"})
            raise
        with transaction(engine) as session:
            result = ingest(
                session, archive, endpoint.name, key, value, settings.timezone, fetched_at=now
            )
        if result["status"] == "stale":
            return
        with transaction(engine) as session:
            record_endpoint_fetch(session, endpoint.name, key, now, result)
        if result["status"] == "error":
            raise ValueError("Normalization failed; source preserved for retry")
    elif kind == "garmin_activities":
        offset = payload["offset"]
        values = reader.call("get_activities", offset, 100)
        with transaction(engine) as session:
            result = ingest(
                session,
                archive,
                "activities",
                f"page:{offset}",
                values,
                settings.timezone,
                fetched_at=now,
            )
        if result["status"] == "error" or not isinstance(values, list):
            raise ValueError("Activity page normalization failed; response archived")
        if result["status"] == "stale":
            return
        with transaction(engine) as session:
            upsert(
                session,
                AppState,
                dict(
                    key=f"freshness:activities:page:{offset}",
                    value={
                        "success_at": now.isoformat(),
                        "status": result["status"],
                        "source_key": f"page:{offset}",
                    },
                ),
                ["key"],
            )
            for activity in values:
                identity = str(activity["activityId"])
                if timestamp(activity["startTimeGMT"]).astimezone(
                    ZoneInfo(settings.timezone)
                ).date() < date.fromisoformat(payload["since"]):
                    continue
                for endpoint in ENDPOINTS:
                    if endpoint.scope == "activity":
                        for delay in (0, 1200):
                            enqueue(
                                session,
                                "garmin_endpoint",
                                {"endpoint": endpoint.name, "key": identity},
                                f"activity:{identity}:{endpoint.name}:{delay}:{now.date()}",
                                now + timedelta(seconds=delay),
                            )
                enqueue(
                    session,
                    "garmin_fit",
                    {"activity_id": identity},
                    f"fit:{identity}:{now.date()}",
                    now,
                )
            if len(values) == 100 and timestamp(values[-1]["startTimeGMT"]).astimezone(
                ZoneInfo(settings.timezone)
            ).date() >= date.fromisoformat(payload["since"]):
                enqueue(
                    session,
                    kind,
                    {**payload, "offset": offset + 100},
                    f"activity-page:{now.date()}:{offset + 100}",
                    now,
                )
    elif kind == "garmin_fit":
        identity = payload["activity_id"]
        raw = reader.call(
            "download_activity", identity, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
        )
        # Archive before parsing so failures never lose the original.
        archive.put_bytes(raw, "zip")
        with transaction(engine) as session:
            result = store_fit(session, archive, identity, raw, fetched_at=now)
        if result["status"] == "error":
            raise ValueError("FIT parsing failed; indexed source retained")
        if result["status"] != "stale":
            with transaction(engine) as session:
                upsert(
                    session,
                    AppState,
                    dict(
                        key=f"freshness:activity_fit:{identity}",
                        value={
                            "success_at": now.isoformat(),
                            "status": result["status"],
                            "source_key": identity,
                        },
                    ),
                    ["key"],
                )
    else:
        raise ValueError("Unknown Garmin job kind")
