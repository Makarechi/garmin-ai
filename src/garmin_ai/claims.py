"""Validate numeric and timestamp citations against exact successful result paths."""

import math
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, StrictFloat, StrictInt, StrictStr

from garmin_ai.events import StrictModel

PathKey = (
    Annotated[StrictStr, Field(min_length=1, max_length=120)]
    | Annotated[StrictInt, Field(ge=0, le=10000)]
)


class NumericClaim(StrictModel):
    evidence_id: Annotated[StrictInt, Field(ge=1)]
    path: list[PathKey] = Field(min_length=1, max_length=12)
    value: StrictInt | StrictFloat


class TimestampClaim(StrictModel):
    evidence_id: Annotated[StrictInt, Field(ge=1)]
    path: list[PathKey] = Field(min_length=1, max_length=12)
    value: Annotated[StrictStr, Field(min_length=1, max_length=80)]


def cited_value(claim, evidence, selected_ids):
    sources = {item["id"]: item for item in evidence if "error" not in item["result"]}
    if claim.evidence_id not in selected_ids or claim.evidence_id not in sources:
        raise ValueError("Claim requires cited successful evidence")
    item = sources[claim.evidence_id]
    value = item["result"]
    for key in claim.path:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            raise ValueError("Claim path does not exist")
    return item, value


def evidence_path(path):
    return "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)


def verified_numbers(claims, evidence, selected_ids):
    lines = []
    for claim in claims:
        item, value = cited_value(claim, evidence, selected_ids)
        if (
            type(value) not in {int, float}
            or (isinstance(value, float) and not math.isfinite(value))
            or value != claim.value
        ):
            raise ValueError("Numeric claim differs from exact source field")
        # Programmatic provenance prevents a free-form model label from silently
        # turning a maximum into a mean, or assigning a different unit.
        path = evidence_path(claim.path)
        lines.append(f"Evidence {claim.evidence_id}: {item['tool']} {path}: {value}")
    return lines


def verified_timestamps(claims, evidence, selected_ids, timezone):
    zone = ZoneInfo(timezone)
    lines = []
    for claim in claims:
        item, value = cited_value(claim, evidence, selected_ids)
        if not isinstance(value, str) or value != claim.value:
            raise ValueError("Timestamp claim differs from exact source field")
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Timestamp claim requires a timezone")
        try:
            local = instant.astimezone(zone)
        except OverflowError as exc:
            raise ValueError("Timestamp is outside the local datetime range") from exc
        label = f"Evidence {claim.evidence_id}: {item['tool']} {evidence_path(claim.path)}"
        if (
            item["tool"] == "analysis_sleep"
            and len(claim.path) == 4
            and claim.path[0] == "rows"
            and type(claim.path[1]) is int
            and claim.path[2] == "main_interval"
            and claim.path[3] in {"start", "end"}
        ):
            label = "Начало сна по Garmin" if claim.path[3] == "start" else "Пробуждение по Garmin"
        lines.append(f"{label}: {local:%d.%m.%Y в %H:%M} ({timezone}, {local:%z}).")
    return lines
