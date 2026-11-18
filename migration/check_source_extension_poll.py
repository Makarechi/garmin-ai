"""Compare a native out-of-process source with the Python source and resume its saved page."""

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from capture_reference import isolated_engine
from check_source_page_storage import install_metric, mapping, snapshot
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai import source_runtime
from garmin_ai.accounts import owner
from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.integrations import PluginContext
from garmin_ai.models import AppState
from garmin_ai.source_metric_mapping import (
    SourceMetricMappingConfirmation,
    confirm_source_metric_mapping,
    preview_source_metric_mapping,
)

sys.path.insert(0, str(Path.cwd() / "examples" / "synthetic-adapters"))
from synthetic_adapters import SampleConfig, SampleSource  # noqa: E402

INSTANCE = "source:sample:one"
CURSOR = f"source-plugin:cursor:{INSTANCE}"
SECRET = "synthetic_source_scope_fixture"
CONFIG = {"label": "Прогулки", "limit": 7.0}


def seeded(engine, settings):
    with Session(engine) as session, session.begin():
        owner(session)
        session.add(
            AppState(key="preferences:onboarding", value={"source_instance_ids": [INSTANCE]})
        )
        session.flush()
        metric = install_metric(session)
        draft = mapping(metric.id)
        preview = preview_source_metric_mapping(session, settings, draft)
        confirm_source_metric_mapping(
            session,
            settings,
            SourceMetricMappingConfirmation(
                draft=draft, confirmation_token=preview["confirmation_token"]
            ),
        )
        session.add(
            AppState(
                key=CURSOR,
                value={
                    "window_start": "2026-01-01T00:00:00+00:00",
                    "window_end": "2026-01-02T00:00:00+00:00",
                    "next_cursor": "0",
                    "seen_cursor_hashes": [],
                    "retry_after": None,
                    "last_completed_at": None,
                },
            )
        )


def native_command(binary, program):
    return [str(binary), "poll-source-extension", str(program), INSTANCE]


def scoped_unfinished_adapter(directory, sample):
    path = directory / "interrupted-adapter"
    program = f"""#!{sys.executable}
import json
import os
import subprocess
import sys

request = json.load(sys.stdin)
assert "GA_DATABASE_URL" not in os.environ and "GA_PLUGIN_UNRELATED" not in os.environ
assert request["context"] == {{"config": {CONFIG!r}, "secrets": {{"fixture": {SECRET!r}}}}}
if request["method"] == "read_page" and request["params"]["cursor"] == "2":
    print("malformed second page")
else:
    child = subprocess.run(
        [{str(sample)!r}], input=json.dumps(request), text=True, capture_output=True, check=True
    )
    sys.stdout.write(child.stdout)
"""
    path.write_text(program, encoding="utf-8")
    path.chmod(0o700)
    return path


def wait_for_commit_pause(engine, process):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and process.poll() is None:
        with engine.connect() as connection:
            sleeping = connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event='PgSleep')"
                )
            )
        if sleeping:
            return
        time.sleep(0.01)
    raise AssertionError("The fixture did not reach the publication transaction")


def verify(binary, sample):
    instance = IntegrationInstance(
        id=INSTANCE,
        kind="source",
        provider="sample",
        config=CONFIG,
        secret_refs={"fixture": "GA_PLUGIN_SYNTHETIC_SOURCE"},
    )
    settings = Settings(_env_file=None, integrations=[instance])
    # Use isolated_engine's normal PostgreSQL and empty-schema checks for both instances.
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    rust_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_url == python_url:
        raise ValueError("The probe requires distinct disposable databases")
    reference = isolated_engine()
    with patch.dict(os.environ, GA_TEST_DATABASE_URL=rust_url):
        native = isolated_engine()
    try:
        for engine in (reference, native):
            seeded(engine, settings)
        source = SampleSource(
            PluginContext(INSTANCE, SampleConfig(label=CONFIG["label"]), MappingProxyType({}))
        )
        registry = SimpleNamespace(
            descriptor=lambda *_: SimpleNamespace(plugin_factory=SampleSource),
            status=lambda *_: SimpleNamespace(available=True),
            create=lambda *_: source,
        )
        with patch.object(source_runtime, "default_registry", return_value=registry):
            expected = source_runtime.poll_source_instance(reference, settings, INSTANCE)
        assert expected == {"status": "complete", "pages": 2, "records": 3}

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            env = {
                "GA_DATABASE_URL": rust_url,
                "GA_INTEGRATIONS": json.dumps(
                    [instance.model_dump(mode="json")], ensure_ascii=False
                ),
                "GA_PLUGIN_SYNTHETIC_SOURCE": SECRET,
                "GA_PLUGIN_UNRELATED": "synthetic_unrelated",
            }

            def run(executable, settings_env=env):
                return subprocess.run(
                    native_command(binary, executable),
                    env=settings_env,
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )

            nonexistent = directory / "unused-executable"
            disabled = run(nonexistent, {**env, "GA_INTEGRATIONS": "[]"})
            assert disabled.returncode == 0
            assert json.loads(disabled.stdout) == {"status": "disabled", "pages": 0, "records": 0}
            with engine_selection(native, []):
                refused = run(nonexistent)
                assert refused.returncode == 0 and json.loads(refused.stdout) == json.loads(
                    disabled.stdout
                )
            until = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            with Session(native) as session, session.begin():
                row = session.get(AppState, CURSOR)
                row.value = {**row.value, "retry_after": until}
            deferred = run(nonexistent)
            assert deferred.returncode == 0 and json.loads(deferred.stdout)["status"] == "deferred"
            with Session(native) as session, session.begin():
                row = session.get(AppState, CURSOR)
                row.value = {**row.value, "retry_after": None}

            stopped = run(scoped_unfinished_adapter(directory, sample))
            assert stopped.returncode == 1 and not stopped.stdout
            assert "invalid response envelope" in stopped.stderr and SECRET not in stopped.stderr
            before = snapshot(native)
            assert len(before[0]) == len(before[1]) == 2
            assert before[2][CURSOR]["next_cursor"] == "2"

            with native.begin() as connection:
                connection.execute(
                    text("""
                    CREATE FUNCTION fixture_pause_cursor() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.key LIKE 'source-plugin:cursor:%' THEN PERFORM pg_sleep(0.5); END IF;
                        RETURN NEW;
                    END $$;
                    CREATE TRIGGER fixture_pause_cursor
                    BEFORE UPDATE ON app_state FOR EACH ROW EXECUTE FUNCTION fixture_pause_cursor();
                """)
                )
            with subprocess.Popen(
                native_command(binary, sample),
                env=env,
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as process:
                wait_for_commit_pause(native, process)
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                assert process.returncode == 1 and not stdout and "was cancelled" in stderr
            assert snapshot(native) == before
            with native.begin() as connection:
                connection.execute(text("DROP TRIGGER fixture_pause_cursor ON app_state"))

            resumed = run(sample)
            assert resumed.returncode == 0, resumed.stderr.replace(SECRET, "<redacted>")
            assert json.loads(resumed.stdout) == {"status": "complete", "pages": 1, "records": 1}
        expected_store = snapshot(reference)
        actual_store = snapshot(native)
        for store in (expected_store, actual_store):
            assert store[2][CURSOR].pop("last_completed_at") is not None
        assert expected_store == actual_store
    finally:
        reference.dispose()
        native.dispose()
    print(
        "Verified source consent, atomic publication, interruption, restart and mapped metrics against Python"
    )


@contextmanager
def engine_selection(engine, allowed):
    def save(items):
        with Session(engine) as session, session.begin():
            row = session.get(AppState, "preferences:onboarding")
            row.value = {"source_instance_ids": items}

    save(allowed)
    try:
        yield
    finally:
        save([INSTANCE])


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("binary", type=Path)
    cli.add_argument("sample", type=Path)
    arguments = cli.parse_args()
    verify(arguments.binary.resolve(strict=True), arguments.sample.resolve(strict=True))
