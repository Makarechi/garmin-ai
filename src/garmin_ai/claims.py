"""Validate numeric citations against exact successful tool-result paths."""

import math
from typing import Annotated

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


def verified_numbers(claims, evidence, selected_ids):
    sources = {item["id"]: item for item in evidence if "error" not in item["result"]}
    lines = []
    for claim in claims:
        if claim.evidence_id not in selected_ids or claim.evidence_id not in sources:
            raise ValueError("Numeric claim requires cited successful evidence")
        item = sources[claim.evidence_id]
        value = item["result"]
        for key in claim.path:
            if isinstance(value, dict) and isinstance(key, str) and key in value:
                value = value[key]
            elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
                value = value[key]
            else:
                raise ValueError("Numeric claim path does not exist")
        if type(value) not in {int, float} or not math.isfinite(value) or value != claim.value:
            raise ValueError("Numeric claim differs from exact source field")
        # Programmatic provenance prevents a free-form model label from silently
        # turning a maximum into a mean, or assigning a different unit.
        path = "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in claim.path)
        lines.append(f"{item['tool']} {path}: {value}")
    return lines
