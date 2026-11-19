"""Compare owner web chat with a synthetic model using a disposable database."""

import argparse
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from check_tracker_extraction_http import certificates
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from garmin_ai.agent import Interpretation
from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.llm import ProviderUnavailable
from garmin_ai.models import Person
from garmin_ai.scenario_packs import configure_scenario_pack, ensure_scenario_packs

TOKENS = [{"key": "synthetic-web-chat-owner-bearer-for-disposable-tests-7042", "scopes": ["admin"]}]
HEADERS = {"Authorization": f"Bearer {TOKENS[0]['key']}"}
INSTANCE = "model:openai:synthetic-web"
MODEL = "synthetic-web-model"
KEY = "synthetic-web-provider-key"
INTEGRATION = {
    "id": INSTANCE,
    "kind": "model",
    "provider": "openai",
    "secret_refs": {"api_key": "GA_PLUGIN_WEB_CHAT_SYNTHETIC"},
    "config": {"model": MODEL, "structured_output": True, "timeout_seconds": 8},
}
CONSENT = {
    "provider": "openai",
    "provider_instance_id": INSTANCE,
    "model": MODEL,
    "categories": ["health", "diary"],
    "policy_revision": 1,
    "granted_at": "2026-01-01T00:00:00Z",
}
NOTE = "Synthetic note for 20 September 2026 at 20:00 UTC"
QUESTION = "What detail should the synthetic note contain?"
DETAIL = "Synthetic model-generated note"
CORRECTED = "Corrected synthetic web note"


def output(prompt):
    sentence = prompt["text"]
    if sentence.startswith("Correct the synthetic note "):
        target = sentence.split()[4]
        return Interpretation(
            intent="update",
            confidence=1,
            response_language="en",
            target_event_id=target,
            changed_fields=["payload.description"],
            events=[
                {
                    "start": "2026-09-20T20:00:00Z",
                    "timezone": "UTC",
                    "payload": {"type": "note", "description": CORRECTED},
                }
            ],
        )
    if sentence.startswith("Close the synthetic migraine "):
        target = sentence.split()[4]
        end = sentence.rsplit(" at ", 1)[1]
        return Interpretation(
            intent="close",
            confidence=1,
            response_language="en",
            target_event_id=target,
            events=[
                {
                    "start": "2026-09-20T20:00:00Z",
                    "end": end,
                    "timezone": "UTC",
                    "payload": {"type": "migraine"},
                }
            ],
        )
    if "synthetic failure" in sentence:
        raise ProviderUnavailable("Synthetic upstream failure")
    if "clarification" in sentence:
        return Interpretation(intent="clarify", confidence=1, clarification=QUESTION)
    if "safety classification" in sentence:
        return Interpretation(intent="safety", confidence=1, response_language="en")
    return Interpretation(
        intent="log",
        confidence=1,
        response_language="en",
        events=[
            {
                "start": "2026-09-20T20:00:00Z",
                "timezone": "UTC",
                "payload": {"type": "note", "description": DETAIL},
            }
        ],
    )


class SyntheticProvider:
    instance_id = INSTANCE

    def __init__(self, calls):
        self.calls = calls

    def structured(self, _instruction, prompt, schema, **_options):
        assert schema is Interpretation
        document = json.loads(prompt)
        self.calls.append(document)
        return output(document)

    def close(self):
        pass


def set_model_categories(engine, categories):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO app_state(key,value) VALUES('preferences:onboarding',"
                "CAST(:policy AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            ),
            {
                "policy": json.dumps(
                    {
                        "channel": {"channel": "web", "instance_id": "local"},
                        "model_categories": categories,
                    }
                )
            },
        )


@contextmanager
def synthetic_https(directory, calls, engine):
    ca, certificate, key = certificates(directory)
    active = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/v1/responses"
            assert self.headers["Authorization"] == f"Bearer {KEY}"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["model"] == MODEL and request["store"] is False
            assert request["text"]["format"]["strict"] is True
            prompt = json.loads(request["input"][1]["content"])
            calls.append(prompt)
            if "concurrent" in prompt["text"]:
                active.set()
                assert release.wait(4), "The local request did not complete during model work"
            if "withdraw during response" in prompt["text"]:
                set_model_categories(engine, ["health"])
            try:
                parsed = output(prompt)
            except ProviderUnavailable:
                self.send_error(503)
                return
            body = json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": parsed.model_dump_json()}],
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    hosts = Path("/etc/hosts")
    line = "127.0.0.1 api.openai.com # synthetic-garmin-web-chat-model-test\n"
    hosts.write_text(hosts.read_text() + "\n" + line)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        assert all(
            ipaddress.ip_address(row[4][0]).is_loopback
            for row in socket.getaddrinfo("api.openai.com", 443)
        )
        thread.start()
        yield ca, active, release
    finally:
        hosts.write_text(hosts.read_text().replace(line, ""))
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()


def submit(api, sentence, identity=None, status=200, headers=HEADERS):
    request = {"client_message_id": identity or str(uuid4()), "text": sentence}
    response = api.post("/web-chat/messages", json=request, headers=headers)
    assert response.status_code == status, (status, response.status_code, response.text)
    return response.json()


def exercise(engine, python, native, source_calls, native_calls, active, release):
    submit(native, NOTE, status=401, headers={})
    assert not native_calls
    assert submit(native, "/help")["reply"]["text"] == submit(python, "/help")["reply"]["text"]
    assert not source_calls and not native_calls

    identity = str(uuid4())
    reference = submit(python, NOTE, identity)
    assert "Saved: note" in reference["reply"]["text"]
    crossed = submit(native, NOTE, identity)
    assert crossed == {**reference, "duplicate": True}
    submit(native, NOTE + " changed", identity, status=409)
    assert len(source_calls) == 1 and not native_calls

    identity = str(uuid4())
    first = submit(native, NOTE, identity)
    assert first["reply"]["text"] == reference["reply"]["text"]
    assert not first["duplicate"]
    second = submit(native, NOTE, identity)
    assert second == {**first, "duplicate": True}
    assert len(native_calls) == 1
    assert native.get("/web-chat/messages", headers=HEADERS).status_code == 200
    read = native.post(f"/web-chat/messages/{first['reply']['id']}/read", headers=HEADERS)
    assert read.status_code == 200 and read.json() == {"status": "read"}

    reference = submit(python, "Synthetic safety classification")
    received = submit(native, "Synthetic safety classification")
    assert received["reply"]["text"] == reference["reply"]["text"]
    assert "112" in received["reply"]["text"]
    safety_notice = reference["reply"]["text"]

    reference = submit(python, "Give a synthetic clarification")
    received = submit(native, "Give a synthetic clarification through Rust")
    assert reference["reply"]["text"] == received["reply"]["text"] == QUESTION
    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT value->>'question', value->>'text' FROM app_state "
                "WHERE key='conversation:pending:web:local'"
            )
        ).one()
        assert stored == (QUESTION, "Give a synthetic clarification through Rust")

    identity = str(uuid4())
    sentence = "Synthetic safety classification concurrent with cancellation"
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(submit, native, sentence, identity)
        try:
            assert active.wait(3)
            pool.submit(submit, native, "/cancel").result(timeout=2)
            assert not pending.done(), "The provider must still be waiting"
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "SELECT value FROM app_state WHERE key='conversation:pending:web:local'"
                        )
                    )
                    is None
                )
        finally:
            release.set()
        raced = pending.result(timeout=8)
    assert raced["reply"]["text"] == safety_notice, raced["reply"]["text"]
    assert submit(python, sentence, identity) == {**raced, "duplicate": True}
    active.clear()
    release.clear()

    identity = str(uuid4())
    before = len(native_calls)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(submit, native, NOTE + " concurrent", identity)
        try:
            assert active.wait(3)
            second = pool.submit(submit, native, NOTE + " concurrent", identity)
            local = pool.submit(submit, native, "/help")
            local.result(timeout=2)
            assert not first.done(), "The provider must still be waiting"
        finally:
            release.set()
        pair = [first.result(timeout=8), second.result(timeout=8)]
    assert sorted(row["duplicate"] for row in pair) == [False, True]
    assert pair[0]["message_id"] == pair[1]["message_id"]
    assert len(native_calls) == before + 1

    exercise_corrections(engine, python, native, native_calls)

    calls_before = len(native_calls)
    withdrawn = submit(native, NOTE + " withdraw during response")
    assert "Ничего не сохранено" in withdrawn["reply"]["text"]
    assert len(native_calls) == calls_before + 1
    set_model_categories(engine, ["health", "diary"])
    reference = submit(python, "synthetic failure")
    received = submit(native, "synthetic failure")
    assert received["reply"]["text"] == reference["reply"]["text"]
    assert "Ничего не сохранено" in received["reply"]["text"]
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE source='manual' AND payload->>'description'=:detail"
                ),
                {"detail": DETAIL},
            )
            == 3
        )
    before = (len(source_calls), len(native_calls))
    set_model_categories(engine, ["health"])
    expected = submit(python, "Synthetic consent withdrawn")["reply"]["text"]
    assert submit(native, "Synthetic consent withdrawn")["reply"]["text"] == expected
    assert "Модель недоступна" in expected
    assert (len(source_calls), len(native_calls)) == before
    print(
        "Paired web responses, shared retries, read confirmation, overlapping local controls, consent and model failure passed"
    )


def exercise_corrections(engine, python, native, native_calls):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=30)
    end = now - timedelta(minutes=2)
    with Session(engine) as session:
        packs = ensure_scenario_packs(session)
        for name in ("general_diary", "migraine"):
            configure_scenario_pack(
                session,
                name,
                {
                    "revision": packs[name].revision,
                    "tracking_enabled": True,
                    "collection_enabled": True,
                    "reminders_enabled": False,
                    "visible": True,
                    "llm_enabled": True,
                },
            )
        notes = [
            create_event(
                session,
                EventInput(
                    start=start,
                    timezone="UTC",
                    source="manual",
                    original_text="Synthetic original note text",
                    payload={"type": "note", "description": "Synthetic note before correction"},
                ),
                actor="synthetic-fixture",
            ).id
            for _ in range(2)
        ]
        episodes = [
            create_event(
                session,
                EventInput(start=start, timezone="UTC", payload={"type": "migraine"}),
                actor="synthetic-fixture",
            ).id
            for _ in range(2)
        ]
        session.commit()

    for pair, prefix in (
        (notes, "Correct the synthetic note"),
        (episodes, "Close the synthetic migraine"),
    ):
        ending = f" at {end.isoformat()}" if pair == episodes else ""
        reference = submit(python, f"{prefix} {pair[0]}{ending}")
        identity = str(uuid4())
        sentence = f"{prefix} {pair[1]}{ending}"
        reviewed = submit(native, sentence, identity)
        assert reviewed["reply"]["text"].startswith("Saved:"), reviewed
        assert reviewed["reply"]["text"] == reference["reply"]["text"]
        calls = len(native_calls)
        replay = submit(native, sentence, identity)
        assert replay == {**reviewed, "duplicate": True}
        assert len(native_calls) == calls
        with engine.connect() as connection:
            rows = [
                connection.execute(
                    text('SELECT revision, payload, "end", original_text FROM events WHERE id=:id'),
                    {"id": event_id},
                ).one()
                for event_id in pair
            ]
            assert rows[0] == rows[1] and rows[0].revision == 2
            for event_id, response in zip(pair, (reference, reviewed), strict=True):
                audit = connection.execute(
                    text(
                        "SELECT a.actor,a.before->>'revision',a.after->>'revision',"
                        "a.operation_id=i.operation_id FROM audit_log a "
                        "JOIN inbound_messages i ON i.id=CAST(:message AS uuid) "
                        "WHERE a.event_id=:event AND a.action='update'"
                    ),
                    {"message": response["message_id"], "event": event_id},
                ).one()
                assert audit == ("web:local", "1", "2", True)
            if pair == notes:
                assert rows[0].payload["description"] == CORRECTED
                assert rows[0].original_text == "Synthetic original note text"
            else:
                assert rows[0].end == end
    unoffered = submit(native, f"Correct the synthetic note {uuid4()}")
    assert "Ничего не сохранено" in unoffered["reply"]["text"]
    print(
        "Matched single-target correction and episode close, with exact audit revisions and retries"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    with engine.begin() as connection:
        result = connection.execute(
            update(Person)
            .where(Person.singleton)
            .values(locale="en", timezone="UTC", units="metric")
        )
        assert result.rowcount == 1
    source_calls, native_calls = [], []
    settings = Settings(
        _env_file=None,
        locale="en",
        timezone="UTC",
        llm_enabled=True,
        integrations=[INTEGRATION],
        api_tokens=TOKENS,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="synthetic-web-model-") as path:
            directory = Path(path)
            with (
                synthetic_https(directory, native_calls, engine) as (ca, active, release),
                patch(
                    "garmin_ai.integrations.create_model_provider",
                    lambda *_args, **_kwargs: SyntheticProvider(source_calls),
                ),
                TestClient(create_app(settings, engine)) as python,
                subprocess.Popen(
                    [args.binary.resolve(strict=True), "serve", "--port", "0"],
                    cwd=directory,
                    env={
                        "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                        "GA_API_TOKENS": json.dumps(TOKENS),
                        "GA_LOCALE": "en",
                        "GA_TIMEZONE": "UTC",
                        "GA_LLM_ENABLED": "true",
                        "GA_INTEGRATIONS": json.dumps([INTEGRATION]),
                        "GA_PLUGIN_WEB_CHAT_SYNTHETIC": KEY,
                        "GA_LLM_CONSENT": json.dumps(CONSENT),
                        "SSL_CERT_FILE": str(ca),
                    },
                    stdout=subprocess.PIPE,
                    text=True,
                ) as server,
            ):
                try:
                    url = server.stdout.readline().strip().removeprefix("Listening on ")
                    assert url.startswith("http://127.0.0.1:")
                    with httpx.Client(base_url=url, timeout=10, trust_env=False) as native:
                        exercise(
                            engine, python, native, source_calls, native_calls, active, release
                        )
                finally:
                    server.terminate()
                    server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
