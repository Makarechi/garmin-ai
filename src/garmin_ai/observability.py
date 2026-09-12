"""Operational counters only: no diary contents or health measurements."""

from datetime import UTC, datetime

from sqlalchemy import case, func, select

from garmin_ai.garmin import ENDPOINTS
from garmin_ai.integration import KEY
from garmin_ai.models import AppState, Job, SourcePayload

JOB_KINDS = frozenset(
    {
        "garmin_endpoint",
        "garmin_activities",
        "garmin_fit",
        "raw_replay",
        "telegram_update",
        "telegram_control",
        "telegram_ack",
        "telegram_failure",
        "telegram_connection_notice",
        "telegram_provider_notice",
        "agent_proactive",
        "agent_insights",
        "backup",
        "storage_check",
        "telegram_storage_notice",
    }
)
JOB_STATUSES = frozenset({"pending", "running", "done", "failed"})
SOURCE_STATUSES = frozenset(
    {
        "pending",
        "normalized",
        "archived",
        "empty",
        "error",
        "stale",
        "partial",
        "unsupported",
        "quarantined",
    }
)
CONNECTION_STATUSES = frozenset({"active", "rate_limited", "degraded", "reauth_required"})


def bounded_label(column, allowed):
    return case((column.in_(sorted(allowed)), column), else_="other")


def backup_capacity(session, now):
    unknown = {"available": False, "age_seconds": None, "sufficient": None, "volumes": []}
    row = session.get(AppState, "storage:backup-capacity")
    if not row or not isinstance(row.value, dict):
        return unknown
    value = row.value
    try:
        at = datetime.fromisoformat(value["at"])
        age = (now - at).total_seconds()
        volumes = value["volumes"]
        if age < 0 or value["status"] not in {"ready", "insufficient"}:
            return unknown
        if not isinstance(volumes, list) or not 1 <= len(volumes) <= 2:
            return unknown
        safe = []
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("role") not in {
                "shared",
                "staging",
                "destination",
            }:
                return unknown
            numbers = [volume.get(key) for key in ("free_bytes", "required_bytes")]
            if any(type(number) is not int or not 0 <= number <= 2**63 - 1 for number in numbers):
                return unknown
            safe.append(
                {"role": volume["role"], "free_bytes": numbers[0], "required_bytes": numbers[1]}
            )
        roles = [volume["role"] for volume in safe]
        if sorted(roles) not in [["shared"], ["destination", "staging"]]:
            return unknown
        sufficient = all(volume["free_bytes"] >= volume["required_bytes"] for volume in safe)
        if sufficient != (value["status"] == "ready"):
            return unknown
        return {"available": True, "age_seconds": age, "sufficient": sufficient, "volumes": safe}
    except (KeyError, TypeError, ValueError, OverflowError):
        return unknown


def snapshot(session, now=None):
    now = now or datetime.now(UTC)
    job_kind = bounded_label(Job.kind, JOB_KINDS)
    job_status = bounded_label(Job.status, JOB_STATUSES)
    endpoint = bounded_label(
        SourcePayload.endpoint, {e.name for e in ENDPOINTS} | {"activities", "activity_fit"}
    )
    source_status = bounded_label(SourcePayload.status, SOURCE_STATUSES)
    connection = session.get(AppState, KEY)
    connection_value = (
        dict(connection.value) if connection and isinstance(connection.value, dict) else {}
    )
    state = connection_value.get("status", "unknown")
    if not isinstance(state, str) or state not in CONNECTION_STATUSES:
        state = "unknown"
    deadline = connection_value.get("blocked_until")
    try:
        blocked = bool(deadline and datetime.fromisoformat(deadline) > now)
    except (ValueError, TypeError, OverflowError):
        blocked = False
        state = "unknown"
    connection_paused = connection_value.get("status") == "reauth_required" or blocked
    lane = case(
        (
            Job.kind.in_(["garmin_endpoint", "garmin_activities", "garmin_fit", "raw_replay"]),
            "garmin",
        ),
        (
            Job.kind.in_(
                [
                    "telegram_update",
                    "telegram_control",
                    "telegram_ack",
                    "telegram_failure",
                    "telegram_connection_notice",
                    "telegram_provider_notice",
                ]
            ),
            "telegram",
        ),
        (Job.kind.in_(["agent_proactive", "agent_insights"]), "analysis"),
        (Job.kind == "backup", "backup"),
        else_="other",
    )
    heartbeat = session.get(AppState, "runtime:heartbeat")
    backup = session.get(AppState, "backup:last_success")
    return {
        "runtime_heartbeat_age_seconds": (
            now - datetime.fromisoformat(heartbeat.value["at"])
        ).total_seconds()
        if heartbeat
        else None,
        "backup_age_seconds": (now - datetime.fromisoformat(backup.value["at"])).total_seconds()
        if backup
        else None,
        "garmin_connection": {"state": state, "paused": connection_paused},
        "backup_capacity": backup_capacity(session, now),
        "queue_due": [
            {
                "lane": label,
                "count": count,
                "oldest_due_age_seconds": max(0, (now - oldest).total_seconds()),
            }
            for label, count, oldest in session.execute(
                select(lane, func.count(), func.min(Job.run_at))
                .where(Job.status == "pending", Job.run_at <= now)
                .group_by(lane)
            )
        ],
        "jobs": [
            {"kind": kind, "status": status, "count": count}
            for kind, status, count in session.execute(
                select(job_kind, job_status, func.count()).group_by(job_kind, job_status)
            )
        ],
        "sources": [
            {"endpoint": endpoint, "status": status, "count": count}
            for endpoint, status, count in session.execute(
                select(endpoint, source_status, func.count()).group_by(endpoint, source_status)
            )
        ],
    }


def prometheus(session):
    data = snapshot(session)
    lines = []
    for metric in ("runtime_heartbeat_age_seconds", "backup_age_seconds"):
        value = data[metric]
        lines.append(f"garmin_ai_{metric} {value if value is not None else -1}")
    for category, label in [("jobs", "kind"), ("sources", "endpoint")]:
        for row in data[category]:
            # Labels originate from internal code, but still escape arbitrary stored values.
            values = {
                k: str(row[k]).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                for k in (label, "status")
            }
            lines.append(
                f'garmin_ai_{category}{{{label}="{values[label]}",status="{values["status"]}"}} {row["count"]}'
            )
    for row in data["queue_due"]:
        for key in ("count", "oldest_due_age_seconds"):
            lines.append(f'garmin_ai_queue_due_{key}{{lane="{row["lane"]}"}} {row[key]}')
    capacity = data["backup_capacity"]
    lines.append(f"garmin_ai_backup_capacity_available {int(capacity['available'])}")
    if capacity["available"]:
        lines.append(f"garmin_ai_backup_capacity_age_seconds {capacity['age_seconds']}")
        lines.append(f"garmin_ai_backup_capacity_sufficient {int(capacity['sufficient'])}")
        for volume in capacity["volumes"]:
            for key in ("free_bytes", "required_bytes"):
                lines.append(
                    f'garmin_ai_backup_capacity_{key}{{role="{volume["role"]}"}} {volume[key]}'
                )
    connection = data["garmin_connection"]
    lines.append(f"garmin_ai_garmin_paused {int(connection['paused'])}")
    for state in sorted(CONNECTION_STATUSES | {"unknown"}):
        lines.append(
            f'garmin_ai_garmin_connection_state{{state="{state}"}} {int(state == connection["state"])}'
        )
    return "\n".join(lines) + "\n"
