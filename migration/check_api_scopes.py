"""Capture pinned HTTP capability and pure tool authorization decisions."""

import argparse
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from garmin_ai.access import TOOL_SCOPES, permits_tool
from garmin_ai.api import create_app
from garmin_ai.config import Settings

ALL_SCOPES = (
    "read:health",
    "read:diary",
    "write:diary",
    "write:wearable",
    "manage:definitions",
    "manage:integrations",
    "admin",
)
SCOPED = [
    {"key": "synthetic-default-health-token-" + "x" * 16},
    {"key": "synthetic-diary-read-token-" + "x" * 16, "scopes": ["read:diary"]},
    {"key": "synthetic-diary-write-token-" + "x" * 16, "scopes": ["write:diary"]},
    {
        "key": "synthetic-diary-editor-token-" + "x" * 16,
        "scopes": ["read:diary", "write:diary", "read:diary"],
    },
    {
        "key": "synthetic-definitions-token-" + "x" * 16,
        "scopes": ["manage:definitions"],
    },
    {
        "key": "synthetic-wearable-token-" + "x" * 16,
        "scopes": ["write:wearable"],
        "wearable_device_id": "11111111-1111-1111-1111-111111111111",
    },
    {"key": "synthetic-empty-scopes-token-" + "x" * 16, "scopes": []},
    {"key": "synthetic-administrator-token-" + "x" * 16, "scopes": ["admin"]},
]
LEGACY = "synthetic-legacy-compatibility-token-" + "x" * 16


def http_cases():
    engine = create_engine("sqlite://")
    try:
        settings = Settings(api_key=LEGACY, api_tokens=SCOPED)
        with TestClient(create_app(settings, engine=engine)) as client:
            headers = [None, "", "bearer " + LEGACY, "Bearer synthetic-unregistered"]
            headers.extend(
                "Bearer " + token for token in [LEGACY, *(item["key"] for item in SCOPED)]
            )
            result = []
            for method, header in itertools.product(["GET", "HEAD", "POST", "OPTIONS"], headers):
                send = {} if header is None else {"Authorization": header}
                response = client.request(method, "/capabilities", headers=send)
                result.append(
                    {
                        "method": method,
                        "authorization": header,
                        "status": response.status_code,
                        "cache_control": response.headers.get("cache-control"),
                        "allow": response.headers.get("allow"),
                        "body": response.text,
                    }
                )
        return result
    finally:
        engine.dispose()


def tool_cases():
    requests = [{"name": name} for name in [*TOOL_SCOPES, "unassigned_tool"]]
    requests.extend(
        {"name": "resource_catalog", "kind": kind}
        for kind in ["events", "tracker_metrics", "system_metrics", "raw_sources"]
    )
    requests.extend(
        {"name": "generic_analysis", "operation": op, "metric": metric}
        for op, metric in [
            ("query_entries", None),
            ("summarize", "user.synthetic"),
            ("summarize", "system.synthetic"),
            ("summarize", None),
        ]
    )
    for bits in range(1 << len(ALL_SCOPES)):
        scopes = [scope for bit, scope in enumerate(ALL_SCOPES) if bits & (1 << bit)]
        for request in requests:
            validated = None
            if "kind" in request:
                validated = SimpleNamespace(kind=request["kind"])
            if "operation" in request:
                validated = SimpleNamespace(
                    spec=SimpleNamespace(
                        operation=request["operation"], metric_key=request["metric"]
                    )
                )
            yield {
                "scopes": scopes,
                **request,
                "permitted": permits_tool(set(scopes), request["name"], validated),
            }


def capture(path):
    document = {
        "legacy": LEGACY,
        "scoped": SCOPED,
        "http": http_cases(),
        "tools": list(tool_cases()),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    print(f"Captured {len(document['http'])} HTTP and {len(document['tools'])} tool cases")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
