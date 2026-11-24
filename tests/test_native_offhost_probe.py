"""Keep the frozen Python fixture and the current native schema boundary distinct."""

import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location(
    "run_native_offhost_probe", ROOT / "scripts/run_native_offhost_probe.py"
)
adapter = module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_native_migration_follows_validated_historical_provisioning(monkeypatch, tmp_path):
    steps = []
    address = "postgresql+psycopg://garmin@127.0.0.1/offhost_test"
    engine = SimpleNamespace(
        url=SimpleNamespace(render_as_string=lambda **_options: address),
        dispose=lambda: steps.append("dispose"),
    )

    def provision():
        steps.append("historical_empty_fixture_validated")
        return engine

    binary = tmp_path / "candidate"

    def run(command, **options):
        assert command == [binary, "migrate"]
        assert options == {"env": {"GA_DATABASE_URL": address}, "check": True, "timeout": 60}
        steps.append("native_migration")

    readiness_check = object()
    probe = SimpleNamespace(engine_for_empty_test_database=provision, native_api=readiness_check)
    monkeypatch.setattr(adapter.subprocess, "run", run)
    adapter.install_native_migration(probe, binary)
    assert probe.engine_for_empty_test_database() is engine
    steps.append("native_api_or_restore")
    assert steps == [
        "historical_empty_fixture_validated",
        "native_migration",
        "native_api_or_restore",
    ]
    assert probe.native_api is readiness_check


def test_native_migration_failure_stops_before_using_the_fixture(monkeypatch, tmp_path):
    disposed = []
    engine = SimpleNamespace(
        url=SimpleNamespace(render_as_string=lambda **_options: "synthetic-test-address"),
        dispose=lambda: disposed.append(True),
    )
    probe = SimpleNamespace(engine_for_empty_test_database=lambda: engine)

    def fail(command, **_options):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(adapter.subprocess, "run", fail)
    adapter.install_native_migration(probe, tmp_path / "candidate")
    with pytest.raises(subprocess.CalledProcessError):
        probe.engine_for_empty_test_database()
    assert disposed == [True]


def test_invalid_frozen_fixture_is_rejected_before_import(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter.subprocess, "check_output", lambda *_args, **_options: "different")
    with pytest.raises(ValueError, match="pinned Python reference"):
        adapter.load_frozen_probe(tmp_path)
