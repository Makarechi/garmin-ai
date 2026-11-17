"""Compare Python and candidate observations from the same synthetic compatibility case."""

import argparse
import json
import math
from pathlib import Path


def pointer(parent, key):
    return f"{parent}/{str(key).replace('~', '~0').replace('/', '~1')}"


def differences(expected, candidate, location=""):
    if type(expected) is not type(candidate):
        yield f"{location}: type {type(expected).__name__} became {type(candidate).__name__}"
    elif isinstance(expected, dict):
        for key in sorted(expected.keys() - candidate.keys()):
            yield f"{pointer(location, key)}: missing"
        for key in sorted(candidate.keys() - expected.keys()):
            yield f"{pointer(location, key)}: unexpected"
        for key in sorted(expected.keys() & candidate.keys()):
            yield from differences(expected[key], candidate[key], pointer(location, key))
    elif isinstance(expected, list):
        if len(expected) != len(candidate):
            yield f"{location}: length {len(expected)} became {len(candidate)}"
        for index, (left, right) in enumerate(zip(expected, candidate, strict=False)):
            yield from differences(left, right, pointer(location, index))
    elif isinstance(expected, float) and expected.hex() != candidate.hex():
        yield f"{location}: value differs"
    elif expected != candidate:
        yield f"{location}: value differs"


def reject_nonfinite(constant):
    raise ValueError(f"Nonfinite JSON number {constant}")


def parse_float(token):
    number = float(token)
    if not math.isfinite(number):
        raise ValueError("JSON number overflows finite double precision")
    return number


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def load(path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_nonfinite,
        parse_float=parse_float,
        object_pairs_hook=unique_object,
    )


def compare(reference, candidate):
    required = {"baseline_sha", "format_version", "implementation", "scenarios"}
    for observation in (reference, candidate):
        if (
            set(observation) != required
            or type(observation["format_version"]) is not int
            or observation["format_version"] != 1
        ):
            raise ValueError("Unexpected observation format")
        if not isinstance(observation["scenarios"], dict) or not observation["scenarios"]:
            raise ValueError("Observation contains no scenarios")
    return list(
        differences(
            {key: reference[key] for key in required - {"implementation"}},
            {key: candidate[key] for key in required - {"implementation"}},
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    reference, candidate = load(args.reference), load(args.candidate)
    changes = compare(reference, candidate)
    print(
        json.dumps(
            {
                "baseline_sha": reference["baseline_sha"],
                "reference": reference["implementation"],
                "candidate": candidate["implementation"],
                "matched": not changes,
                "differences": changes,
            },
            indent=2,
        )
    )
    return 1 if changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
