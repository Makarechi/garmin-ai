"""Operational counters only: no diary contents or health measurements."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from garmin_ai.models import AppState, Job, SourcePayload


def snapshot(session):
    now = datetime.now(UTC)
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
        "jobs": [
            {"kind": kind, "status": status, "count": count}
            for kind, status, count in session.execute(
                select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)
            )
        ],
        "sources": [
            {"endpoint": endpoint, "status": status, "count": count}
            for endpoint, status, count in session.execute(
                select(SourcePayload.endpoint, SourcePayload.status, func.count()).group_by(
                    SourcePayload.endpoint, SourcePayload.status
                )
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
    return "\n".join(lines) + "\n"
