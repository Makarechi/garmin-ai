"""Generate synthetic user metric contracts and hashes from pinned Python."""

import argparse
import json
from pathlib import Path

from garmin_ai.metric_definitions import MetricSpec, metric_hash

BASE = {
    "key": "user.synthetic_metric",
    "labels": {"en": "Synthetic 🧪"},
    "value_kind": "physical_number",
    "unit": "m/s",
    "dimension": "speed",
    "aggregation": "latest",
    "allowed_methods": ["latest", "mean"],
    "coverage": {"kind": "all_values"},
    "time_semantics": "point",
    "minimum": -0.0,
    "maximum": 200,
}


def variants():
    yield BASE
    for key in [
        "none",
        "system.synthetic_metric",
        "user.Invalid",
        "user.synthetic-short",
        "user." + "a" * 128,
    ]:
        yield BASE | {"key": key}
    for method in ["sum", "median", "latest", "distribution", "mean", "delta"]:
        yield BASE | {"aggregation": method, "allowed_methods": [method, method]}
    for bounds in [(0, None), (True, "1.5"), ("0.3", "-1"), (-5, 200), (0, "Infinity"), ("nan", 5)]:
        yield BASE | {"minimum": bounds[0], "maximum": bounds[1]}
    for unit, dimension in [
        ("m/s", "speed"),
        ("km/h", "speed"),
        ("s/km", "pace"),
        ("s/km", "speed"),
        ("%", "ratio"),
        ("score_١-١٠", "ordinal"),
        ("score_Ⅳ-Ⅴ", "ordinal"),
        ("score_-3--1", "ordinal"),
        ("score_0-0", "ordinal"),
        ("score_1-7", "ordinal"),
        ("L", "volume"),
        ("mg", "mass"),
        ("none", "duration"),
        (None, "speed"),
        ("g", "mass"),
    ]:
        yield BASE | {"unit": unit, "dimension": dimension}
    for time in ["point", "interval", "calendar_period", None]:
        yield BASE | {"time_semantics": time}
    for kind in ["all_values", "sparse", "time_weighted", "other"]:
        for ratio, gap in [
            (None, None),
            (0.8, 60),
            (0, 86400),
            (1, 86401),
            ("0.5", "1.0"),
            (True, "1e2"),
            (1.01, 10),
            (0.8, None),
            (None, 2),
        ]:
            yield BASE | {
                "coverage": {"kind": kind, "minimum_ratio": ratio, "max_gap_seconds": gap}
            }
    for scale in ["valid.scale", "Invalid.scale", None, "a" * 128, "a" * 129]:
        for version in [1, 0, 1.0, True, None, "1.0", "1e0"]:
            yield BASE | {
                "value_kind": "ordinal",
                "unit": "score",
                "dimension": "ordinal",
                "scale_id": scale,
                "scale_version": version,
                "aggregation": "median",
                "allowed_methods": ["median", "latest"],
            }
    categorical = BASE | {
        "value_kind": "nominal",
        "unit": None,
        "dimension": "category",
        "aggregation": "mode",
        "allowed_methods": ["latest", "mode", "counts"],
        "minimum": None,
        "maximum": None,
    }
    for values in [
        None,
        [],
        ["A", "B", "🧪"],
        ["A", "A"],
        [" "],
        ["a" * 200],
        ["a" * 201],
        [1],
        ["\x1f"],
    ]:
        yield categorical | {"category_domain": values}
    yield categorical | {"minimum": 0}
    yield categorical | {"unit": "1"}
    yield categorical | {"scale_id": "scale"}
    yield categorical | {
        "value_kind": "boolean",
        "aggregation": "rate",
        "allowed_methods": ["rate", "count_true"],
    }
    for label in ["", " ", "\x1f", "🧪" * 120, "🧪" * 121, True]:
        yield BASE | {"labels": {"en": label}}


def expected(document):
    try:
        spec = MetricSpec.model_validate(document)
    except (TypeError, ValueError):
        return None
    if not spec.key.startswith("user."):
        return None
    contract = spec.model_dump(mode="json")
    contract["allowed_methods"] = sorted(contract["allowed_methods"])
    return [spec.key, metric_hash(spec), contract]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    corpus = [[document, expected(document)] for document in variants()]
    args.output.write_text(json.dumps(corpus, ensure_ascii=True, allow_nan=False), encoding="utf-8")
