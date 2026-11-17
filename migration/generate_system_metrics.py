"""Print the canonical built-in metric specs from the pinned Python runtime."""

import json
from unittest.mock import patch

from garmin_ai import metric_definitions as metrics


class CatalogOnlySession:
    def execute(self, statement):
        if str(statement) != "SELECT pg_advisory_xact_lock(72104629)":
            raise ValueError("Unexpected database operation during metric catalog generation")


def export():
    recorded = {}

    def record(_session, spec, *, namespace):
        assert namespace == "system"
        contract = spec.model_dump(mode="json")
        contract["allowed_methods"] = sorted(contract["allowed_methods"])
        recorded[spec.key.removeprefix("system.")] = {
            "hash": metrics.metric_hash(spec),
            "contract": contract,
        }

    with patch.object(metrics, "_upsert_metric_definition", side_effect=record):
        metrics.ensure_system_metric_definitions(CatalogOnlySession())
    return recorded


if __name__ == "__main__":
    print(json.dumps(export(), ensure_ascii=True, indent=2, sort_keys=True))
