import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp import types
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from garmin_ai.access import TOOL_SCOPES
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.mcp_server import WRITES, build_server
from garmin_ai.models import Event
from garmin_ai.tools import TOOLS


def test_all_tools_have_explicit_access_policy():
    assert set(TOOLS) == set(TOOL_SCOPES)


@pytest.mark.parametrize("name", list(WRITES))
def test_readonly_mcp_rejects_direct_writes_before_arguments_or_database(name):
    server = build_server(None)
    request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments={}))
    result = asyncio.run(server.request_handlers[types.CallToolRequest](request)).root
    assert result.isError
    assert "PermissionError" in result.content[0].text
    listed = asyncio.run(
        server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    ).root
    assert not set(WRITES).intersection(t.name for t in listed.tools)


@pytest.mark.parametrize(
    "scopes",
    [
        set(),
        {"read:health"},
        {"read:diary"},
        {"write:diary"},
        {"read:diary", "write:diary"},
        {"admin"},
    ],
)
def test_api_enforces_discovery_and_direct_call_scopes(db, db_engine, scopes, monkeypatch):
    from garmin_ai import api

    calls = []
    monkeypatch.setattr(api, "call_tool", lambda session, name, arguments: calls.append(name) or {})
    key = "synthetic-scoped-api-credential-123456"
    client = TestClient(
        create_app(Settings(api_tokens=[ApiToken(key=key, scopes=scopes)]), db_engine)
    )
    headers = {"Authorization": "Bearer " + key}
    expected = {
        name for name, required in TOOL_SCOPES.items() if "admin" in scopes or required <= scopes
    }
    assert {t["name"] for t in client.get("/tools", headers=headers).json()} == expected
    for name in TOOLS:
        response = client.post("/tools/" + name, headers=headers, json={"arguments": {}})
        assert response.status_code == (200 if name in expected else 403)
    assert set(calls) == expected
    for path in ("/metrics", "/operations"):
        assert client.get(path, headers=headers).status_code == (200 if "admin" in scopes else 403)
    body = {
        "start": "2026-09-10T12:00:00Z",
        "payload": {"type": "note", "description": "synthetic"},
    }
    allowed_write = "admin" in scopes or {"read:diary", "write:diary"} <= scopes
    assert client.post("/events", headers=headers, json=body).status_code == (
        200 if allowed_write else 403
    )
    assert db.scalar(select(func.count()).select_from(Event)) == int(allowed_write)


def test_health_key_cannot_read_or_mutate_diary_by_id(db, db_engine):
    admin, health = "a" * 32, "h" * 32
    client = TestClient(
        create_app(Settings(api_key=SecretStr(admin), api_tokens=[ApiToken(key=health)]), db_engine)
    )
    body = {
        "start": "2026-09-10T12:00:00Z",
        "payload": {"type": "note", "description": "synthetic"},
    }
    created = client.post("/events", headers={"Authorization": "Bearer " + admin}, json=body).json()
    headers = {"Authorization": "Bearer " + health}
    path = "/events/" + created["id"]
    assert client.get(path, headers=headers).status_code == 403
    assert client.put(path, headers=headers, json={"revision": 1, "event": body}).status_code == 403
    assert client.delete(path, headers=headers, params={"revision": 1}).status_code == 403
    assert client.get("/tools", headers={"Authorization": "Bearer " + "z" * 32}).status_code == 401
    assert client.get("/tools").status_code == 401


def test_duplicate_weak_and_unknown_scope_credentials_are_rejected():
    with pytest.raises(ValidationError):
        ApiToken(key="short")
    with pytest.raises(ValidationError):
        ApiToken(key="x" * 32, scopes={"read:everything"})
    with pytest.raises(ValidationError):
        Settings(api_key="x" * 32, api_tokens=[ApiToken(key="x" * 32)])
    with pytest.raises(ValidationError):
        Settings(api_tokens=[ApiToken(key="x" * 32), ApiToken(key="x" * 32)])
