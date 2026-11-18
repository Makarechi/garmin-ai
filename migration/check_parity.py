"""Validate the migration inventory, test references and optional completion gate."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

MIGRATION = Path(__file__).resolve().parent
ROOT = MIGRATION.parent
STATUSES = {"not_started", "implemented", "verified", "blocked"}
LEVELS = {"none", "static", "unit", "db", "e2e_synthetic", "live", "operational"}


def inventory_groups(inventory):
    telegram = inventory["telegram"]
    return {
        "http": inventory["http"]["routes"],
        "cli": inventory["cli"]["commands"],
        "tools": set(inventory["mcp"]["reads"]) | set(inventory["mcp"]["opt_in_writes"]),
        "jobs": inventory["job_kinds"],
        "telegram_commands": telegram["commands"],
        "telegram_actions": set(telegram["callbacks"]) | set(telegram["dynamic_actions"]),
        "web_commands": inventory["web_commands"],
        "garmin": inventory["garmin"],
        "settings": inventory["environment_keys"],
        "tables": inventory["tables"],
        "migrations": inventory["migration_revisions"],
        "event_types": inventory["system_event_types"],
        "metrics": inventory["system_metrics"],
        "packs": inventory["system_packs"],
        "integrations": inventory["integrations"],
        "operations": inventory["operational_files"],
        "universal": inventory["universal_acceptance"],
        "agent": inventory["agent_acceptance"],
    }


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(inventory, parity, nodeids, *, require_verified=False):
    owners = defaultdict(list)
    identifiers = Counter()
    errors = []
    pending = []
    pytest_files = {nodeid.partition("::")[0] for nodeid in nodeids}
    pytest_functions = {nodeid.partition("[")[0] for nodeid in nodeids}

    for row in parity["entries"]:
        identity = row["id"]
        identifiers[identity] += 1
        if row["baseline_sha"] != inventory["baseline_sha"]:
            errors.append(f"{identity} has a different baseline")
        for field in (
            "source_modules",
            "entrypoints",
            "baseline_tests",
            "data_contracts",
            "permissions",
            "owner_task",
        ):
            if not row[field]:
                errors.append(f"{identity} is missing {field}")
        if row["status"] not in STATUSES:
            errors.append(f"{identity} has an unknown status")
        if row["verification"] not in LEVELS:
            errors.append(f"{identity} has an unknown verification level")
        if row["status"] == "verified" and (not row["rust_tests"] or row["verification"] == "none"):
            errors.append(f"{identity} claims verification without Rust evidence")
        if row["status"] != "verified":
            pending.append(identity)
        for module in row["source_modules"]:
            if not (ROOT / module).is_file():
                errors.append(f"{identity} refers to missing source {module}")
        for test in row["baseline_tests"]:
            if test not in nodeids and test not in pytest_functions and test not in pytest_files:
                errors.append(f"{identity} refers to uncollected test {test}")
        for test in row["rust_tests"]:
            path = Path(test.partition("::")[0])
            if path.is_absolute() or ".." in path.parts or not (ROOT / path).is_file():
                errors.append(f"{identity} refers to missing Rust evidence {test}")
        for kind, names in row["entrypoints"].items():
            for name in names:
                owners[kind, name].append(identity)

    for identity, count in identifiers.items():
        if count != 1:
            errors.append(f"{identity} is repeated")
    groups = inventory_groups(inventory)
    expected = {(kind, name) for kind, names in groups.items() for name in names}
    for kind, name in sorted(expected - owners.keys()):
        errors.append(f"Unassigned {kind}: {name}")
    for kind, name in sorted(owners.keys() - expected):
        errors.append(f"Unknown {kind}: {name}")
    for (kind, name), assigned in owners.items():
        if len(assigned) != 1:
            errors.append(f"{kind}: {name} is assigned to {', '.join(assigned)}")
    for identity, paths in inventory["universal_acceptance"].items():
        for path in paths:
            if path not in nodeids and path not in pytest_functions:
                errors.append(f"{identity} refers to uncollected test {path}")
    if require_verified and pending:
        errors.append("Parity verification is incomplete")

    return {
        "baseline_sha": inventory["baseline_sha"],
        "areas": len(identifiers),
        "surface_counts": {kind: len(names) for kind, names in groups.items()},
        "total_surfaces": len(expected),
        "unverified_areas": pending,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", type=Path, default=MIGRATION / "contracts/baseline-surfaces.json"
    )
    parser.add_argument("--parity", type=Path, default=MIGRATION / "parity.yaml")
    parser.add_argument("--nodeids", type=Path, default=MIGRATION / "baseline-test-nodeids.txt")
    parser.add_argument("--require-verified", action="store_true")
    args = parser.parse_args()
    result = validate(
        load_json(args.inventory),
        load_json(args.parity),
        set(args.nodeids.read_text(encoding="utf-8").splitlines()),
        require_verified=args.require_verified,
    )
    print(json.dumps(result, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
