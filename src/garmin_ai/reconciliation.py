"""Explicit adapter attestations for authoritative interval replacement."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, String, cast, delete, func, select, update

from garmin_ai.models import (
    AppState,
    Insight,
    Measurement,
    MeasurementRevision,
    PendingQuestion,
    SourcePayload,
)
from garmin_ai.projection_changes import execute_projection

ENDPOINT_METRICS = {
    "heart_rate": {"heart_rate_bpm"},
    "stress": {"stress_score", "body_battery"},
    "hrv": {"hrv_rmssd_ms"},
    "respiration": {"respiration_rpm"},
    "spo2": {"spo2_pct"},
    "steps": {"steps_bucket"},
}


@dataclass(frozen=True)
class Replacement:
    start: datetime
    end: datetime
    metrics: tuple[str, ...]
    evidence: str

    def validate(self, endpoint):
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Replacement bounds require timezones")
        if (
            not timedelta(0)
            < self.end.astimezone(UTC) - self.start.astimezone(UTC)
            <= timedelta(days=31)
        ):
            raise ValueError("Replacement interval must be positive and at most 31 days")
        if not self.metrics or not set(self.metrics) <= ENDPOINT_METRICS.get(endpoint, set()):
            raise ValueError("Replacement channels do not match endpoint")
        if not self.evidence or len(self.evidence) > 200:
            raise ValueError("Adapter completeness evidence is required")

    def serialize(self):
        return {
            "start": self.start.astimezone(UTC).isoformat(),
            "end": self.end.astimezone(UTC).isoformat(),
            "metrics": sorted(set(self.metrics)),
            "evidence": self.evidence,
        }

    @classmethod
    def restore(cls, value):
        return (
            cls(
                datetime.fromisoformat(value["start"]),
                datetime.fromisoformat(value["end"]),
                tuple(value["metrics"]),
                value["evidence"],
            )
            if value
            else None
        )


def replace_interval(session, source, endpoint, key, replacement):
    previous = select(SourcePayload.id).where(
        SourcePayload.source == source,
        SourcePayload.endpoint == endpoint,
        SourcePayload.source_key == key,
    )
    execute = (
        session.execute
        if session.info.get("replacement_snapshot") is not None
        else lambda stmt: execute_projection(session, stmt)
    )
    replaced_filter = (
        Measurement.source_ref.in_(previous),
        Measurement.metric.in_(replacement.metrics),
        Measurement.ts >= replacement.start,
        Measurement.ts < replacement.end,
    )
    learned_at = session.info.get("fetch_time") or session.scalar(
        select(func.max(SourcePayload.fetched_at)).where(
            SourcePayload.source == source,
            SourcePayload.endpoint == endpoint,
            SourcePayload.source_key == key,
        )
    )
    if learned_at is None:
        raise ValueError("Replacement source provenance is unavailable")
    for row in session.scalars(select(Measurement).where(*replaced_filter).with_for_update()):
        existing_tombstone = session.scalar(
            select(MeasurementRevision.id).where(
                MeasurementRevision.ts == row.ts,
                MeasurementRevision.metric == row.metric,
                MeasurementRevision.source == row.source,
                MeasurementRevision.source_ref == row.source_ref,
                MeasurementRevision.ingested_at == learned_at,
                MeasurementRevision.deleted.is_(True),
            )
        )
        if existing_tombstone is not None:
            continue
        session.add(
            MeasurementRevision(
                ts=row.ts,
                metric=row.metric,
                source=row.source,
                local_date=row.local_date,
                value=row.value,
                unit=row.unit,
                metric_definition_version_id=row.metric_definition_version_id,
                source_ref=row.source_ref,
                quality=row.quality,
                details=row.details,
                ingested_at=learned_at,
                deleted=True,
            )
        )
    session.flush()
    execute(delete(Measurement).where(*replaced_filter))

    session.execute(
        delete(AppState).where(
            AppState.key.startswith("sample-owner:"),
            AppState.value["source_ref"].astext.in_(
                select(cast(SourcePayload.id, String)).where(
                    SourcePayload.source == source,
                    SourcePayload.endpoint == endpoint,
                    SourcePayload.source_key == key,
                )
            ),
            AppState.value["metric"].astext.in_(replacement.metrics),
            cast(AppState.value["ts"].astext, DateTime(timezone=True)) >= replacement.start,
            cast(AppState.value["ts"].astext, DateTime(timezone=True)) < replacement.end,
        )
    )


def interval_projection(session, source, replacement):
    return set(
        session.execute(
            select(
                Measurement.ts,
                Measurement.metric,
                Measurement.source,
                Measurement.value,
                Measurement.unit,
                Measurement.local_date,
            ).where(
                (Measurement.source == source)
                | Measurement.source_ref.in_(
                    select(SourcePayload.id).where(SourcePayload.source == source)
                ),
                Measurement.metric.in_(replacement.metrics),
                Measurement.ts >= replacement.start,
                Measurement.ts < replacement.end,
            )
        ).all()
    )


def invalidate_insights(session, endpoint, timezone):
    session.execute(
        update(Insight)
        .where(Insight.status.in_(["candidate", "accepted", "delivered", "uncertain"]))
        .values(status="superseded")
    )

    if endpoint not in {"heart_rate", "stress"}:
        return
    from garmin_ai.proactive import context_physiology

    now = datetime.now(UTC)
    for question in session.scalars(
        select(PendingQuestion).where(
            PendingQuestion.kind == "context", PendingQuestion.status == "pending"
        )
    ):
        try:
            left = datetime.fromisoformat(question.evidence["start"])
            right = datetime.fromisoformat(question.evidence["end"])
            evidence = context_physiology(
                session, question.evidence.get("timezone", timezone), now, left, right
            )
        except (KeyError, ValueError, TypeError):
            evidence = None
        if evidence is None:
            question.status = "cancelled"
        else:
            question.evidence = {**question.evidence, **evidence}
