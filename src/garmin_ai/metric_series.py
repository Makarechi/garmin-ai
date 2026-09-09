"""Bounded, source-separated aggregation using canonical metric contracts."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from garmin_ai.metrics import CATALOG, contract
from garmin_ai.models import Measurement


def series(session, metric, start, end, minutes, limit, *, origin=None):
    spec = CATALOG[metric]
    width = minutes * 60
    offset = origin.timestamp() if origin else 0
    query = select(Measurement).where(
        Measurement.metric == metric,
        Measurement.unit == spec.unit,
        Measurement.quality == "observed",
        Measurement.value >= spec.minimum,
        Measurement.value < float("inf"),
        Measurement.ts >= start - timedelta(seconds=spec.max_gap_seconds),
        Measurement.ts <= end + timedelta(seconds=spec.max_gap_seconds),
    )
    if spec.maximum is not None:
        query = query.where(Measurement.value <= spec.maximum)
    samples = session.scalars(
        query.order_by(Measurement.source, Measurement.ts).limit(100001)
    ).all()
    if len(samples) > 100000:
        raise ValueError("Series exceeds 100000 samples; request a shorter time range")
    buckets = {}

    def bucket(at, source):
        ts = datetime.fromtimestamp(int((at.timestamp() - offset) // width) * width + offset, UTC)
        return buckets.setdefault(
            (ts, source),
            {
                "ts": ts.isoformat(),
                "source": source,
                "samples": 0,
                "values": [],
                "weighted": 0,
                "covered_seconds": 0,
                "source_refs": set(),
            },
        ), ts

    for row in samples:
        if start <= row.ts < end:
            item, _ = bucket(row.ts, row.source)
            item["samples"] += 1
            item["values"].append(row.value)
            if row.source_ref:
                item["source_refs"].add(str(row.source_ref))
    if spec.aggregation == "time_weighted_mean":
        for a, b in zip(samples, samples[1:], strict=False):
            if (
                a.source != b.source
                or not 0 < (b.ts - a.ts).total_seconds() <= spec.max_gap_seconds
            ):
                continue
            left, right = max(a.ts, start), min(b.ts, end)
            while left < right:
                item, boundary = bucket(left, a.source)
                stop = min(right, boundary + timedelta(seconds=width))
                seconds = (stop - left).total_seconds()
                item["weighted"] += seconds * a.value
                item["covered_seconds"] += seconds
                for row in (a, b):
                    if row.source_ref:
                        item["source_refs"].add(str(row.source_ref))
                left = stop
    result = []
    for (ts, _), item in sorted(buckets.items()):
        values = item.pop("values")
        weighted = item.pop("weighted")
        denominator = (min(end, ts + timedelta(seconds=width)) - max(start, ts)).total_seconds()
        ratio = item["covered_seconds"] / denominator
        mean = weighted / item["covered_seconds"] if ratio >= 0.8 else None
        total = sum(values) if spec.aggregation == "sum" else None
        refs = sorted(item["source_refs"])
        item.update(
            value=total if spec.aggregation == "sum" else mean,
            sum=total,
            mean=None if spec.aggregation == "sum" else mean,
            min=min(values) if values else None,
            max=max(values) if values else None,
            unit=spec.unit,
            aggregation=spec.aggregation,
            coverage_ratio=None if spec.kind == "increment" else ratio,
            source_refs=refs[:100],
            source_refs_truncated=len(refs) > 100,
        )
        if spec.kind == "increment":
            item["covered_seconds"] = None
        result.append(item)
    return {
        "metric": metric,
        "contract": contract(metric),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bucket_minutes": minutes,
        "truncated": len(result) > limit,
        "rows": result[:limit],
        "limitations": [
            "Sources are separate; overlapping providers are not added together",
            "Gauges use bounded left-hold intervals and require 80% coverage",
            "Increment buckets are selected by source interval start",
        ],
    }
