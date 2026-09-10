"""Explicit adapter attestations for authoritative interval replacement."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from garmin_ai.models import Insight, Measurement, SourcePayload

ENDPOINT_METRICS = {
    "heart_rate": {"heart_rate_bpm"},
    "stress": {"stress_score", "body_battery"},
    "body_battery": {"body_battery"},
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
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Replacement bounds require timezones")
        if not timedelta(0) < self.end - self.start <= timedelta(days=31):
            raise ValueError("Replacement interval must be positive and at most 31 days")
        if not self.metrics or not set(self.metrics) <= ENDPOINT_METRICS.get(endpoint, set()):
            raise ValueError("Replacement channels do not match endpoint")
        if not self.evidence or len(self.evidence) > 200:
            raise ValueError("Adapter completeness evidence is required")

    def serialize(self):
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "metrics": list(self.metrics),
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
    session.execute(
        delete(Measurement).where(
            Measurement.source == source,
            Measurement.source_ref.in_(previous),
            Measurement.metric.in_(replacement.metrics),
            Measurement.ts >= replacement.start,
            Measurement.ts < replacement.end,
        )
    )


def invalidate_insights(session):
    session.execute(
        update(Insight)
        .where(Insight.status.in_(["candidate", "accepted", "delivered"]))
        .values(status="superseded")
    )
