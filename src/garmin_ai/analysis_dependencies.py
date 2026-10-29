"""Minimal, revalidatable evidence proofs for queued generic metric answers."""

import hashlib
import json
from datetime import UTC, datetime, timedelta


def _comparable(value):
    if isinstance(value, dict):
        return {
            key: _comparable(child) if key in {"first", "second"} else child
            for key, child in value.items()
            if key not in {"spec_hash", "knowledge_cutoff"}
        }
    return value


def _digest(result):
    content = json.dumps(_comparable(result), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def metric_dependency(item, request_time):
    if item.get("tool") != "generic_analysis" or "error" in item.get("result", {}):
        return None
    result = item["result"]
    if "input_revisions" in result:
        snapshots = [result]
    elif all(isinstance(result.get(key), dict) for key in ("first", "second")):
        snapshots = [result["first"], result["second"]]
        if not all("input_revisions" in snapshot for snapshot in snapshots):
            return None
    elif item.get("arguments", {}).get("spec", {}).get("operation") in {
        "query_entries",
        "query_observations",
        "query_completeness",
    }:
        snapshots = []
    else:
        return None
    return {
        "arguments": item["arguments"],
        "request_time": request_time.isoformat(),
        "result_hash": _digest(result),
        "snapshots": [
            {key: snapshot[key] for key in ("metric", "metric_version", "input_revisions")}
            for snapshot in snapshots
        ],
    }


def metric_dependency_current(session, dependency):
    from garmin_ai.generic_analytics import evidence_is_stale
    from garmin_ai.llm import compact
    from garmin_ai.tools import TOOLS, call_tool

    try:
        request_time = datetime.fromisoformat(dependency["request_time"])
        if request_time.tzinfo is None or request_time.utcoffset() is None:
            return False
        arguments = TOOLS["generic_analysis"].arguments.model_validate(dependency["arguments"])
        spec = arguments.spec
        current_question = spec.knowledge_cutoff >= request_time - timedelta(minutes=5)
        if current_question:
            spec = spec.model_copy(
                update={"knowledge_cutoff": max(spec.knowledge_cutoff, datetime.now(UTC))}
            )
        refreshed = call_tool(
            session, "generic_analysis", {"spec": spec.model_dump(mode="json")}, for_model=True
        )
        refreshed = json.loads(compact(refreshed))
        if _digest(refreshed) != dependency["result_hash"]:
            return False
        snapshots = dependency["snapshots"]
        if not isinstance(snapshots, list) or len(snapshots) > 2:
            return False
        if not snapshots and spec.operation not in {
            "query_entries",
            "query_observations",
            "query_completeness",
        }:
            return False
        return not current_question or not any(evidence_is_stale(session, row) for row in snapshots)
    except (
        AttributeError,
        KeyError,
        LookupError,
        PermissionError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return False
