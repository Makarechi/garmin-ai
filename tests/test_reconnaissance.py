import inspect
import json
import stat
from datetime import date
from types import SimpleNamespace

import pytest
from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError

from garmin_ai.archive import LocalArchive
from garmin_ai.garmin import ENDPOINTS, AuthenticationRequired, CircuitOpen, GarminReader
from garmin_ai.probe import probe, shape


def test_registry_matches_installed_upstream():
    for endpoint in ENDPOINTS:
        method = getattr(Garmin, endpoint.method)
        args = (
            [] if endpoint.scope == "global" else ["2026-09-07" if endpoint.scope == "day" else "1"]
        )
        inspect.signature(method).bind(None, *args)


def test_archive_replay_and_traversal(tmp_path):
    archive = LocalArchive(tmp_path / "raw")
    key = archive.put_json({"b": 2, "a": 1})
    assert key == archive.put_json({"a": 1, "b": 2})
    assert json.loads(archive.read(key)) == {"a": 1, "b": 2}
    assert stat.S_IMODE((archive.root / key).stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        archive.read("../../outside")


def test_response_shapes_remove_scalar_values():
    result = json.dumps(
        shape({"email": "private@example.invalid", "heart_rate": 87, "items": [1, "secret"]})
    )
    assert "private@" not in result and "secret" not in result and "87" not in result


def test_retry_and_auth_circuit():
    calls = []

    def intermittent(day):
        calls.append(day)
        if len(calls) < 3:
            raise GarminConnectConnectionError("private upstream payload")
        return {"totalSteps": 42}

    reader = GarminReader(SimpleNamespace(get_stats=intermittent), sleep=lambda _: None)
    assert reader.call("get_stats", "2026-09-07") == {"totalSteps": 42}
    assert len(calls) == 3

    def bad_login(day):
        raise GarminConnectAuthenticationError("secret")

    reader.client.get_stats = bad_login
    with pytest.raises(AuthenticationRequired):
        reader.call("get_stats", "2026-09-07")
    with pytest.raises(CircuitOpen):
        reader.call("get_stats", "2026-09-07")


def test_write_methods_rejected():
    reader = GarminReader(SimpleNamespace())
    with pytest.raises(ValueError):
        reader.call("delete_activity", "1")


def test_probe_isolates_endpoint_failure_and_keeps_raw(tmp_path):
    class Client:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                if name == "get_sleep_data":
                    raise ValueError("response with secrets")
                if name == "get_activities":
                    return []
                return {"value": 12}

            return call

    archive = LocalArchive(tmp_path)
    report = probe(GarminReader(Client(), interval=0), archive, date(2026, 9, 1), date(2026, 9, 1))
    rows = {r["endpoint"]: r for r in report["requests"]}
    assert rows["sleep"]["status"] == "error"
    assert rows["hrv"]["status"] == "available"
    assert json.loads(archive.read(rows["hrv"]["archive_key"])) == {"value": 12}
    assert "secrets" not in json.dumps(report)
