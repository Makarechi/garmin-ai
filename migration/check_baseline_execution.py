"""Verify the frozen Python suite and its universal scenarios from a pytest JUnit report."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree

from check_parity import MIGRATION, load_json

# Use the same credential redaction as the committed collection inventory.
DATABASE_PASSWORD = re.compile(r"(postgresql(?:\+psycopg)?://[^:/\s]+:)[^@/\s]+(@)")


def case_key(nodeid):
    path, *parts = nodeid.split("::")
    classname = ".".join((path.removesuffix(".py").replace("/", "."), *parts[:-1]))
    return classname, parts[-1]


def outcome(case):
    for name in ("error", "failure", "skipped"):
        if case.find(name) is not None:
            return name
    return "passed"


def check_execution(inventory, nodeids, junit, metadata, revision):
    expected = Counter(case_key(node) for node in nodeids)
    observed = defaultdict(list)
    for case in ElementTree.fromstring(junit).iter("testcase"):
        name = DATABASE_PASSWORD.sub(r"\1<redacted>\2", case.attrib["name"])
        observed[(case.attrib["classname"], name)].append(outcome(case))
    observed_counts = Counter({key: len(outcomes) for key, outcomes in observed.items()})
    missing = sum((expected - observed_counts).values())
    unexpected = sum((observed_counts - expected).values())
    totals = Counter(status for outcomes in observed.values() for status in outcomes)
    skipped_modules = Counter()
    for classname, name in expected:
        skipped_modules[classname] += observed.get((classname, name), ()).count("skipped")

    by_function = defaultdict(set)
    for classname, name in expected:
        by_function[(classname, name.partition("[")[0])].add((classname, name))
    scenarios = {}
    for identity, references in inventory["universal_acceptance"].items():
        cases = set()
        unresolved = []
        for ref in references:
            key = case_key(ref)
            matching = {key} if key in expected else by_function.get(key, set())
            if not matching:
                unresolved.append(ref)
            cases.update(matching)
        scenarios[identity] = {
            "expected_cases": sum(expected[case] for case in cases),
            "passed_cases": sum(observed.get(case, ()).count("passed") for case in cases),
            "missing_references": unresolved,
        }

    errors = []
    if metadata.get("git_sha") != inventory["baseline_sha"]:
        errors.append("The reference metadata identifies a different baseline revision")
    if metadata.get("database_revision") != revision:
        errors.append("The reference metadata identifies a different database revision")
    if metadata.get("profile") != "full" or metadata.get("live_services_used") is not False:
        errors.append("The reference run must use the full synthetic profile")
    if missing or unexpected:
        errors.append("JUnit executions differ from the frozen collection inventory")
    if totals["failure"] or totals["error"]:
        errors.append("The reference suite contains failing or errored tests")
    pending = [
        identity
        for identity, result in scenarios.items()
        if result["expected_cases"] == 0
        or result["expected_cases"] != result["passed_cases"]
        or result["missing_references"]
    ]
    if pending:
        errors.append("Some universal scenarios lack complete passing reference evidence")
    return {
        "baseline_sha": inventory["baseline_sha"],
        "database_revision": revision,
        "junit_sha256": hashlib.sha256(junit).hexdigest(),
        "expected_cases": sum(expected.values()),
        "missing_cases": missing,
        "unexpected_cases": unexpected,
        "passed_cases": totals["passed"],
        "failed_cases": totals["failure"],
        "errored_cases": totals["error"],
        "skipped_cases": totals["skipped"],
        "skipped_by_module": {module: count for module, count in skipped_modules.items() if count},
        "unverified_universal_scenarios": pending,
        "universal_scenarios": scenarios,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("metadata", type=Path)
    args = parser.parse_args()
    report = check_execution(
        load_json(MIGRATION / "contracts/baseline-surfaces.json"),
        (MIGRATION / "baseline-test-nodeids.txt").read_text(encoding="utf-8").splitlines(),
        args.junit.read_bytes(),
        load_json(args.metadata),
        load_json(MIGRATION / "baseline.json")["database"]["alembic_head"],
    )
    print(json.dumps(report, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
