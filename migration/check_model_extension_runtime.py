"""Exercise a selected native model through the normal web chat endpoint."""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from check_web_chat_model import NOTE, TOKENS, set_model_categories, submit
from sqlalchemy import text

INSTANCE = "model:synthetic:diary"
MODEL = "synthetic-extension-model"
KEY_NAME = "GA_PLUGIN_NATIVE_SYNTHETIC_MODEL_KEY"
KEY = "synthetic-native-model-secret"
DETAIL = "Synthetic native extension note"


@contextmanager
def native_server(binary, root, engine, adapter, log, *, register=True, provider="synthetic"):
    integration = {
        "id": INSTANCE,
        "kind": "model",
        "provider": "synthetic",
        "config": {"model": MODEL, "invocation_log": str(log), "release": str(root / "release")},
        "secret_refs": {"api_key": KEY_NAME},
    }
    consent = {
        "provider": provider,
        "provider_instance_id": INSTANCE,
        "model": MODEL,
        "categories": ["health", "diary"],
        "policy_revision": 1,
        "granted_at": "2026-01-01T00:00:00Z",
    }
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_API_TOKENS": json.dumps(TOKENS),
        "GA_LOCALE": "en",
        "GA_TIMEZONE": "UTC",
        "GA_LLM_ENABLED": "true",
        "GA_INTEGRATIONS": json.dumps([integration]),
        KEY_NAME: KEY,
        "GA_LLM_CONSENT": json.dumps(consent),
    }
    if register:
        env["GA_NATIVE_MODEL_PLUGINS"] = json.dumps({INSTANCE: str(adapter)})
    with subprocess.Popen(
        [binary, "serve", "--port", "0"], cwd=root, env=env, stdout=subprocess.PIPE, text=True
    ) as server:
        try:
            url = server.stdout.readline().strip().removeprefix("Listening on ")
            assert url.startswith("http://127.0.0.1:")
            with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
                yield client
        finally:
            server.terminate()
            server.wait(timeout=5)


def verify(binary, adapter, root, engine):
    set_model_categories(engine, ["health", "diary"])
    log = root / "invocations.txt"
    for register, provider in ((False, "synthetic"), (True, "wrong-provider")):
        with native_server(
            binary, root, engine, adapter, log, register=register, provider=provider
        ) as client:
            result = submit(client, NOTE)
            assert "Модель недоступна" in result["reply"]["text"]
        assert not log.exists()

    with native_server(binary, root, engine, adapter, log) as client:
        invoked = ["describe", "structured"]
        submit(client, "/help")
        assert not log.exists()
        identity = str(uuid4())
        recorded = submit(client, NOTE, identity)
        assert "Saved: note" in recorded["reply"]["text"], recorded["reply"]["text"]
        assert submit(client, NOTE, identity) == {**recorded, "duplicate": True}
        assert log.read_text().splitlines() == invoked

        invalid = submit(client, "A synthetic invalid extension output")
        assert "Ничего не сохранено" in invalid["reply"]["text"]
        assert log.read_text().splitlines() == invoked * 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(submit, client, NOTE + " hold extension output")
            try:
                deadline = time.monotonic() + 2
                while len(log.read_text().splitlines()) < 6 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert log.read_text().splitlines() == invoked * 3
                assert not pending.done()
                set_model_categories(engine, ["health"])
            finally:
                (root / "release").touch()
            assert "Ничего не сохранено" in pending.result(timeout=5)["reply"]["text"]
        withdrawn = submit(client, NOTE + " with diary consent withdrawn")
        assert "Модель недоступна" in withdrawn["reply"]["text"]
        assert log.read_text().splitlines() == invoked * 3

    with engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE source='manual' AND payload->>'description'=:text"
            ),
            {"text": DETAIL},
        )
        assert count == 1
        audit = connection.scalar(
            text(
                "SELECT count(*) FROM audit_log a JOIN events e ON e.id=a.event_id "
                "WHERE a.actor='web:local' AND a.action='create' AND e.payload->>'description'=:text"
            ),
            {"text": DETAIL},
        )
        assert audit == 1
    print(
        "Verified model selection, scoped secrets, a durable web log, one retry, invalid output and in-flight revocation"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("adapter", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(
                args.binary.resolve(strict=True),
                args.adapter.resolve(strict=True),
                Path(directory),
                engine,
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
