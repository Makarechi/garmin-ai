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
from datetime import UTC, date, datetime, time, timedelta
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

from garmin_ai.agent import AgentStep, Interpretation
from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event, update_event
from garmin_ai.llm import ProviderUnavailable
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import Event, HealthDay, Measurement, MetricObservation, Person, SourcePayload
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
DIARY_QUESTION = "Which detail is in the synthetic diary for 19 September?"
DIARY_ANSWER = "The diary lists the synthetic detail."
DIARY_START = datetime(2026, 9, 19, 20, tzinfo=UTC)
DIARY_DETAIL = "Synthetic detail from the saved diary"
HEALTH_QUESTION = "Compare synthetic daily Garmin values."
FRESHNESS_QUESTION = "How recent are the synthetic Garmin measurements?"
GENERIC_QUESTION = "Read the synthetic note through generic analysis."
GENERIC_START = datetime(2026, 10, 9, 16, tzinfo=UTC)
GENERIC_DETAIL = "Synthetic historical analysis entry"
AGGREGATE_QUESTION = "Show the synthetic step aggregate."
MEASUREMENT_START = datetime(2026, 10, 10, 14, tzinfo=UTC)
MEASUREMENT_SOURCE = "synthetic-historical-aggregate"
STATISTICS_QUESTION = "Compare the synthetic lags and matched controls."
STATISTICS_START = date(2026, 11, 2)
STATISTICS_END = STATISTICS_START + timedelta(days=13)
HEALTH_READS = {
    "personal_baseline": {"metric": "sleep_score", "start": "2026-09-19", "end": "2026-09-20"},
    "analysis_compare_periods": {
        "metric": "sleep_score",
        "a_start": "2026-09-19",
        "a_end": "2026-09-20",
        "b_start": "2026-09-21",
        "b_end": "2026-09-22",
    },
}


def output(prompt):
    if "question" in prompt:
        if prompt["question"].startswith(HEALTH_QUESTION):
            return health_output(prompt)
        if prompt["question"].startswith(FRESHNESS_QUESTION):
            return freshness_output(prompt)
        if prompt["question"].startswith(GENERIC_QUESTION):
            return generic_output(prompt)
        if prompt["question"].startswith(AGGREGATE_QUESTION):
            return aggregate_output(prompt)
        if prompt["question"].startswith(STATISTICS_QUESTION):
            return statistics_output(prompt)
        assert prompt["question"].startswith(DIARY_QUESTION)
        if prompt["evidence"]:
            evidence = prompt["evidence"][0]
            assert evidence["id"] == 1 and evidence["tool"] == "events"
            assert DIARY_DETAIL in json.dumps(evidence["result"])
            return AgentStep(answer=DIARY_ANSWER, evidence_ids=[1], response_language="en")
        assert any(tool["name"] == "events" for tool in prompt["tools"])
        return AgentStep(
            calls=[
                {
                    "name": "events",
                    "arguments_json": json.dumps(
                        {
                            "start": (DIARY_START - timedelta(minutes=1)).isoformat(),
                            "end": (DIARY_START + timedelta(minutes=1)).isoformat(),
                            "kind": "note",
                        }
                    ),
                }
            ]
        )
    sentence = prompt["text"]
    if sentence.startswith("Undo"):
        confidence = 0.84 if "synthetic low confidence" in sentence else 1
        return Interpretation(intent="undo", confidence=confidence, response_language="en")
    if sentence.startswith(
        (
            DIARY_QUESTION,
            HEALTH_QUESTION,
            FRESHNESS_QUESTION,
            GENERIC_QUESTION,
            AGGREGATE_QUESTION,
            STATISTICS_QUESTION,
        )
    ):
        return Interpretation(intent="question", confidence=1, response_language="en")
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


def health_output(prompt):
    evidence = prompt["evidence"]
    if not evidence:
        tools = {tool["name"]: tool["schema"] for tool in prompt["tools"]}
        assert all("metric" in tools[tool]["required"] for tool in HEALTH_READS)
        return AgentStep(
            calls=[
                {"name": tool, "arguments_json": json.dumps(arguments)}
                for tool, arguments in HEALTH_READS.items()
            ],
            response_language="en",
        )
    assert [row["tool"] for row in evidence] == list(HEALTH_READS)
    if prompt["question"].endswith("without pack permission"):
        assert all(set(row["result"]) == {"error"} for row in evidence)
        return AgentStep(response_language="en")
    assert evidence[0]["result"]["mean"] == 70
    assert evidence[1]["result"]["difference"] == 20
    return AgentStep(
        answer="These are the two requested synthetic aggregates.",
        response_language="en",
        evidence_ids=[1, 2],
        numeric_claims=[
            {"evidence_id": 1, "path": ["mean"], "value": 70},
            {"evidence_id": 2, "path": ["difference"], "value": 20},
        ],
    )


def freshness_output(prompt):
    replaying = prompt["question"].endswith(" during replay")
    if replaying or prompt.get("planning_only"):
        assert prompt["quality_context"] == {}
    else:
        assert set(prompt["quality_context"]) == {"sleep_score"}
        assert prompt["quality_context"]["sleep_score"]["semantics"] == "daily_summary"
    if not prompt["evidence"]:
        assert any(tool["name"] == "data_freshness" for tool in prompt["tools"])
        return AgentStep(calls=[{"name": "data_freshness", "arguments_json": "{}"}])
    [evidence] = prompt["evidence"]
    assert evidence["tool"] == "data_freshness"
    assert set(evidence["result"]["channels"]) == (set() if replaying else {"sleep_score"})
    assert "endpoints" not in evidence["result"] and "archive_replay" not in evidence["result"]
    return AgentStep(
        answer="The stored report provides the age of the available observations.",
        evidence_ids=[1],
        include_current_state_freshness=True,
        response_language="en",
    )


def generic_output(prompt):
    if prompt["evidence"]:
        evidence = prompt["evidence"][0]
        assert evidence["tool"] == "generic_analysis"
        if prompt["question"].endswith(" without pack permission"):
            assert set(evidence["result"]) == {"error"}, evidence["result"]
            return AgentStep(response_language="en")
        assert "error" not in evidence["result"], evidence["result"]
        rows = evidence["result"]["rows"]
        assert len(rows) == 1 and rows[0]["payload"]["description"] == GENERIC_DETAIL, rows
        return AgentStep(
            answer="The stored synthetic note is present.",
            evidence_ids=[1],
            response_language="en",
        )
    tools = {tool["name"]: tool["schema"] for tool in prompt["tools"]}
    assert "spec" in tools["generic_analysis"]["required"]
    spec = {
        "operation": "query_entries",
        "definition_key": "system.note",
        "start": (GENERIC_START - timedelta(seconds=1)).isoformat(),
        "end": (GENERIC_START + timedelta(seconds=1)).isoformat(),
        "limit": 10,
        "knowledge_cutoff": prompt["now"],
    }
    return AgentStep(
        calls=[{"name": "generic_analysis", "arguments_json": json.dumps({"spec": spec})}],
        response_language="en",
    )


def aggregate_output(prompt):
    from_event = prompt["question"].endswith(" through a saved event")
    if not prompt["evidence"]:
        spec = {
            "operation": "aggregate_metric",
            "metric_key": "system.steps_bucket",
            "source": "event" if from_event else f"measurement:{MEASUREMENT_SOURCE}",
            "start": MEASUREMENT_START.isoformat(),
            "end": (MEASUREMENT_START + timedelta(hours=1)).isoformat(),
            "knowledge_cutoff": prompt["now"],
            "limit": 20,
        }
        return AgentStep(
            calls=[{"name": "generic_analysis", "arguments_json": json.dumps({"spec": spec})}],
            response_language="en",
        )
    result = prompt["evidence"][0]["result"]
    assert "error" not in result and result["value"] is not None, result
    assert len(result["input_revisions"]) == (1 if from_event else 2), result
    return AgentStep(
        answer="The synthetic step aggregate is available.",
        evidence_ids=[1],
        numeric_claims=[{"evidence_id": 1, "path": ["value"], "value": result["value"]}],
        response_language="en",
    )


def statistics_output(prompt):
    common = {"start": STATISTICS_START.isoformat(), "end": STATISTICS_END.isoformat()}
    lagged = "analysis_lagged_association"
    migraine = "analysis_migraine_windows"
    if not prompt["evidence"]:
        tools = {entry["name"] for entry in prompt["tools"]}
        assert lagged in tools
        if prompt["question"].endswith(" without the migraine pack"):
            assert migraine not in tools
            return AgentStep(response_language="en")
        assert migraine in tools
        return AgentStep(
            calls=[
                {
                    "name": lagged,
                    "arguments_json": json.dumps(
                        {
                            **common,
                            "metric_a": "steps",
                            "metric_b": "sleep_score",
                            "lags": [-1, 0, 1],
                        }
                    ),
                },
                {
                    "name": migraine,
                    "arguments_json": json.dumps(
                        {
                            **common,
                            "metric": "sleep_score",
                        }
                    ),
                },
            ],
            response_language="en",
        )
    first, second = prompt["evidence"]
    assert first["tool"] == lagged and second["tool"] == migraine
    assert [row["n"] for row in first["result"]["results"]] == [13, 14, 13]
    assert second["result"]["matched_pairs"] == 1 and second["result"]["episodes"] == 1
    [pair] = second["result"]["pairs"]
    assert pair["event_day"] == "2026-11-04" and pair["control_day"] == "2026-11-11"
    return AgentStep(
        answer="The stored synthetic records contain matched daily observations.",
        evidence_ids=[1, 2],
        response_language="en",
    )


class SyntheticProvider:
    instance_id = INSTANCE

    def __init__(self, calls):
        self.calls = calls

    def structured(self, _instruction, prompt, schema, **_options):
        document = json.loads(prompt)
        self.calls.append(document)
        result = output(document)
        assert type(result) is schema
        return result

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
            message = prompt.get("text", "")
            if "concurrent" in message:
                active.set()
                assert release.wait(4), "The local request did not complete during model work"
            if "withdraw during response" in message:
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
    exercise_undo(engine, python, native, native_calls, active, release)
    exercise_analysis(engine, python, native, native_calls)
    exercise_health_analysis(engine, python, native)
    exercise_generic_analysis(engine, python, native)
    exercise_legacy_aggregate(engine, python, native)
    exercise_current_freshness(engine, python, native)
    exercise_statistics(engine, python, native)

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


def exercise_undo(engine, python, native, native_calls, active, release):
    query = text(
        "SELECT e.revision,e.deleted,ARRAY(SELECT action FROM audit_log WHERE event_id=e.id "
        "ORDER BY id) AS actions FROM events e WHERE original_text=:label"
    )

    def state(label):
        with engine.connect() as connection:
            return connection.execute(query, {"label": label}).one()

    sentence = "Undo the last web diary entry"
    origin = "Synthetic reference undo entry"
    target = "Synthetic native undo entry"
    submit(python, f"/note {origin}")
    low_confidence = "Undo with synthetic low confidence"
    clarification = submit(python, low_confidence)["reply"]["text"]
    assert state(origin) == (1, False, ["create"])
    expected = submit(python, sentence)
    submit(native, f"/note {target}")
    response = submit(native, low_confidence)
    assert response["reply"]["text"] == clarification, response
    assert state(target) == (1, False, ["create"])
    foreign = "Synthetic separate API entry"
    with Session(engine) as session, session.begin():
        create_event(
            session,
            EventInput(
                start=datetime.now(UTC),
                timezone="UTC",
                source="manual",
                original_text=foreign,
                payload={"type": "note", "description": foreign},
            ),
            actor="api",
        )
    message = str(uuid4())
    undone = submit(native, sentence, message)
    assert undone["reply"]["text"] == expected["reply"]["text"] == "Last change undone."
    assert state(origin) == state(target) == (2, True, ["create", "undo"])
    assert state(foreign) == (1, False, ["create"])
    calls = len(native_calls)
    assert submit(native, sentence, message) == {**undone, "duplicate": True}
    assert len(native_calls) == calls
    assert "Ничего не сохранено" in submit(native, sentence)["reply"]["text"]
    assert state(target) == (2, True, ["create", "undo"])

    previous = "Synthetic prior entry before delayed undo"
    newer = "Synthetic local entry during delayed undo"
    submit(native, f"/note {previous}")
    active.clear()
    release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(submit, native, "Undo concurrent web diary change")
        try:
            assert active.wait(3)
            assert "Заметка сохранена" in submit(native, f"/note {newer}")["reply"]["text"]
        finally:
            release.set()
        result = waiting.result(timeout=5)
    assert "Ничего не сохранено" in result["reply"]["text"]
    assert state(previous) == state(newer) == (1, False, ["create"])
    print(
        "Matched Python undo and low-confidence clarification; preserved separate API entries, "
        "duplicate requests and newer web changes"
    )


def exercise_analysis(engine, python, native, native_calls):
    with Session(engine) as session:
        event_id = create_event(
            session,
            EventInput(
                start=DIARY_START,
                timezone="UTC",
                payload={"type": "note", "description": DIARY_DETAIL},
            ),
            actor="synthetic-fixture",
        ).id
        session.commit()

    reference = submit(python, DIARY_QUESTION)
    before = len(native_calls)
    identity = str(uuid4())
    answer = submit(native, DIARY_QUESTION, identity)
    assert answer["reply"]["text"] == reference["reply"]["text"], (answer, reference)
    assert answer["reply"]["text"] == f"{DIARY_ANSWER}\n\nBased on saved diary data.", answer
    assert len(native_calls) == before + 3
    assert submit(native, DIARY_QUESTION, identity) == {**answer, "duplicate": True}
    assert len(native_calls) == before + 3
    reply_id = answer["reply"]["id"]
    with engine.connect() as connection:
        turns = text(
            "SELECT state->'analysis_turns' FROM conversations "
            "WHERE channel='web' AND channel_instance_id='local'"
        )
        assert connection.scalar(turns) is None
    read = native.post(f"/web-chat/messages/{reply_id}/read", headers=HEADERS)
    assert read.status_code == 200 and read.json() == {"status": "read"}
    assert native.post(f"/web-chat/messages/{reply_id}/read", headers=HEADERS).status_code == 200
    with engine.connect() as connection:
        retained = connection.scalar(turns)
        assert len(retained) == 1 and retained[0]["question"] == DIARY_QUESTION
        assert len(retained[0]["specs"]) == 1 and retained[0]["specs"][0]["tool"] == "events"

    followup = DIARY_QUESTION + " Check again."
    retry_id = str(uuid4())
    before = len(native_calls)
    stale = submit(native, followup, retry_id)
    assert stale["reply"]["text"] == answer["reply"]["text"]
    context = native_calls[before + 1]["conversation"]["turns"]
    assert len(context) == 1 and context[0]["question"] == DIARY_QUESTION
    assert "answer" not in context[0] and not native_calls[before + 1]["evidence"]
    assert retained[0]["specs"] == context[0]["specs"]
    stale_id = stale["reply"]["id"]
    with Session(engine) as session:
        update_event(
            session,
            event_id,
            EventInput(
                start=DIARY_START,
                timezone="UTC",
                payload={"type": "note", "description": "Synthetic correction to the saved diary"},
            ),
            revision=1,
            actor="synthetic-fixture",
        )
        session.commit()
    history = native.get("/web-chat/messages", headers=HEADERS)
    assert history.status_code == 200, history.text
    history_ids = {reply["id"] for reply in history.json()["replies"]}
    assert reply_id in history_ids and stale_id not in history_ids
    rejected = native.post(f"/web-chat/messages/{stale_id}/read", headers=HEADERS)
    assert rejected.status_code == 409, rejected.text
    assert submit(native, followup, retry_id)["reply"] is None
    assert submit(python, followup, retry_id)["reply"] is None
    assert python.post(f"/web-chat/messages/{stale_id}/read", headers=HEADERS).status_code == 409
    old_history = python.get("/web-chat/messages", headers=HEADERS)
    assert old_history.status_code == 200, old_history.text
    assert stale_id not in {reply["id"] for reply in old_history.json()["replies"]}
    with engine.connect() as connection:
        receipts = connection.execute(
            text(
                "SELECT outbox_message_id::text,state FROM message_delivery_receipts "
                "WHERE outbox_message_id IN (CAST(:fresh AS uuid), CAST(:stale AS uuid))"
            ),
            {"fresh": reply_id, "stale": stale_id},
        ).all()
        assert receipts == [(reply_id, "read")], receipts
    print(
        "Matched grounded web diary answer, retained context after one receipt, "
        "and rejected stale evidence in Rust and the rollback reader"
    )


def exercise_health_analysis(engine, python, native):
    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session)["sleep"].llm_enabled = True
        for day, value in zip(range(19, 23), [60, 80, 40, 60], strict=True):
            session.add(HealthDay(day=date(2026, 9, day), sleep_score=value))

    reference = submit(python, HEALTH_QUESTION)
    identity = str(uuid4())
    reply = submit(native, HEALTH_QUESTION, identity)["reply"]
    assert reply["text"] == reference["reply"]["text"], (reply, reference)
    assert "Garmin" in reply["text"] and "/difference: 20" in reply["text"]
    reply_id = reply["id"]
    with engine.begin() as connection:
        connection.execute(text("UPDATE health_days SET sleep_score=65 WHERE day='2026-09-19'"))
    history = native.get("/web-chat/messages", headers=HEADERS)
    assert history.status_code == 200 and reply_id not in {
        row["id"] for row in history.json()["replies"]
    }
    assert native.post(f"/web-chat/messages/{reply_id}/read", headers=HEADERS).status_code == 409
    assert submit(native, HEALTH_QUESTION, identity)["reply"] is None

    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session)["sleep"].llm_enabled = False
    denied_question = HEALTH_QUESTION + " without pack permission"
    withheld = submit(native, denied_question)["reply"]
    reference = submit(python, denied_question)["reply"]
    assert withheld["text"] == reference["text"], (withheld, reference)
    assert "The analysis could not be completed" in withheld["text"], withheld
    print("Matched cited daily Garmin aggregates and withheld stale or disallowed evidence")


def exercise_current_freshness(engine, python, native):
    at = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        packs = ensure_scenario_packs(session)
        for key in ("training", "wellbeing"):
            packs[key].llm_enabled = False
        packs["sleep"].llm_enabled = True
        session.add(HealthDay(day=at.date(), sleep_score=80))

    reference = submit(python, FRESHNESS_QUESTION)["reply"]
    result = submit(native, FRESHNESS_QUESTION)["reply"]
    assert reference["text"] == result["text"], (reference, result)
    assert "Data recency at the time of your question:" in result["text"]
    assert f"summary for {at.date()}" in result["text"]
    assert "Heart rate" not in result["text"] and "Training readiness" not in result["text"]
    result_id = result["id"]
    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session)["sleep"].llm_enabled = False
    hidden = native.get("/web-chat/messages", headers=HEADERS)
    assert hidden.status_code == 200
    assert result_id not in {reply["id"] for reply in hidden.json()["replies"]}
    assert native.post(f"/web-chat/messages/{result_id}/read", headers=HEADERS).status_code == 409

    source_id = uuid4()
    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session)["sleep"].llm_enabled = True
        session.add(
            SourcePayload(
                id=source_id,
                source="garmin_connect",
                endpoint="stress",
                source_key=at.date().isoformat(),
                payload_hash="a" * 64,
                archive_key="synthetic/replay-pending.json",
                payload={},
                fetched_at=at,
                parser_version=1,
                status="normalized",
            )
        )
    result = native.get("/web-chat/messages", headers=HEADERS)
    assert result.status_code == 200, result.text
    assert not any(reply["id"] == result_id for reply in result.json()["replies"]), result.text
    question = FRESHNESS_QUESTION + " during replay"
    reference = submit(python, question)["reply"]
    result = submit(native, question)["reply"]
    assert reference["text"] == result["text"], (reference, result)
    assert "being recalculated" in result["text"]
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM source_payloads WHERE id=:id"), {"id": source_id})
    print("Matched stored freshness while withholding denied channels and pending replay evidence")


def exercise_generic_analysis(engine, python, native):
    def synthetic_entry(description):
        return EventInput(
            start=GENERIC_START,
            timezone="UTC",
            payload={"type": "note", "description": description},
        )

    # The shared interpretation step currently requires both provider categories.
    set_model_categories(engine, ["health", "diary"])
    try:
        with Session(engine) as session, session.begin():
            ensure_scenario_packs(session)["general_diary"].llm_enabled = True
            entry = create_event(
                session, synthetic_entry(GENERIC_DETAIL), actor="synthetic-fixture"
            )
            identity = entry.id

        expected = submit(python, GENERIC_QUESTION)["reply"]
        request_id = str(uuid4())
        fresh = submit(native, GENERIC_QUESTION, request_id)["reply"]
        assert fresh["text"] == expected["text"], (fresh, expected)
        assert (
            fresh["text"]
            == "The stored synthetic note is present.\n\nBased on saved Garmin and diary data."
        ), fresh

        def history_ids():
            result = native.get("/web-chat/messages", headers=HEADERS)
            assert result.status_code == 200, result.text
            return {row["id"] for row in result.json()["replies"]}

        legacy_id = expected["id"]
        legacy_path = f"/web-chat/messages/{legacy_id}/read"
        assert legacy_id in history_ids()
        with engine.begin() as lock:
            lock.execute(text("SELECT pg_advisory_xact_lock_shared(72104619)"))
            assert legacy_id not in history_ids()
            deferred = native.post(legacy_path, headers=HEADERS)
            assert deferred.status_code == 503, deferred.text
            assert (
                lock.scalar(
                    text(
                        "SELECT count(*) FROM message_delivery_receipts "
                        "WHERE outbox_message_id=CAST(:id AS uuid)"
                    ),
                    {"id": legacy_id},
                )
                == 0
            )
        for _ in range(2):
            assert native.post(legacy_path, headers=HEADERS).status_code == 200
        with engine.connect() as connection:
            confirmations = (
                connection.execute(
                    text(
                        "SELECT state FROM message_delivery_receipts "
                        "WHERE outbox_message_id=CAST(:id AS uuid)"
                    ),
                    {"id": legacy_id},
                )
                .scalars()
                .all()
            )
        assert confirmations == ["read"], confirmations
        stale_legacy = submit(python, GENERIC_QUESTION + " before a correction")["reply"]["id"]
        assert stale_legacy in history_ids()

        with Session(engine) as session, session.begin():
            update_event(
                session,
                identity,
                synthetic_entry("Synthetic correction after the generic answer"),
                revision=1,
                actor="synthetic-fixture",
            )
        history = native.get("/web-chat/messages", headers=HEADERS)
        assert history.status_code == 200 and fresh["id"] not in {
            row["id"] for row in history.json()["replies"]
        }
        assert (
            native.post(f"/web-chat/messages/{fresh['id']}/read", headers=HEADERS).status_code
            == 409
        )
        assert stale_legacy not in history_ids()
        assert (
            native.post(f"/web-chat/messages/{stale_legacy}/read", headers=HEADERS).status_code
            == 409
        )
        assert submit(native, GENERIC_QUESTION, request_id)["reply"] is None
        with Session(engine) as session, session.begin():
            ensure_scenario_packs(session)["general_diary"].llm_enabled = False
        denied = GENERIC_QUESTION + " without pack permission"
        expected = submit(python, denied)["reply"]["text"]
        assert submit(native, denied)["reply"]["text"] == expected
        assert "The analysis could not be completed" in expected, expected
    finally:
        set_model_categories(engine, ["health", "diary"])
    print("Matched generic diary reads and withheld stale or disallowed generic evidence")
    print("Verified legacy generic query history, replay protection and one read receipt")


def exercise_legacy_aggregate(engine, python, native):
    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session)["training"].llm_enabled = True
        contract = ensure_system_metric_definitions(session)["steps_bucket"]
        for minute, value in ((0, 20), (5, 30)):
            at = MEASUREMENT_START + timedelta(minutes=minute)
            session.add(
                Measurement(
                    ts=at,
                    metric="steps_bucket",
                    source=MEASUREMENT_SOURCE,
                    local_date=at.date(),
                    value=value,
                    unit=contract.unit,
                    metric_definition_version_id=contract.id,
                    quality="observed",
                    details={},
                    ingested_at=MEASUREMENT_START - timedelta(days=1),
                )
            )

    def shown(identity):
        result = native.get("/web-chat/messages", headers=HEADERS)
        assert result.status_code == 200, result.text
        return identity in {row["id"] for row in result.json()["replies"]}

    answer = submit(python, AGGREGATE_QUESTION)["reply"]
    assert "/value: 50" in answer["text"], answer
    assert shown(answer["id"])
    assert (
        native.post(f"/web-chat/messages/{answer['id']}/read", headers=HEADERS).status_code == 200
    )
    pending = submit(python, AGGREGATE_QUESTION + " before correction")["reply"]
    assert shown(pending["id"])
    with engine.begin() as connection:
        connection.execute(
            update(Measurement)
            .where(Measurement.ts == MEASUREMENT_START, Measurement.source == MEASUREMENT_SOURCE)
            .values(value=35)
        )
    assert not shown(pending["id"])
    assert (
        native.post(f"/web-chat/messages/{pending['id']}/read", headers=HEADERS).status_code == 409
    )
    with Session(engine) as session, session.begin():
        entry = create_event(
            session,
            EventInput(
                start=MEASUREMENT_START,
                timezone="UTC",
                payload={"type": "note", "description": "Synthetic source event"},
            ),
            actor="synthetic-fixture",
        )
        event_id = entry.id
        contract = ensure_system_metric_definitions(session)["steps_bucket"]
        recorded = datetime.now(UTC) - timedelta(seconds=1)
        session.add(
            MetricObservation(
                source_entry_id=event_id,
                metric="steps_bucket",
                metric_definition_version_id=contract.id,
                value=40,
                unit=contract.unit,
                observed_at=MEASUREMENT_START,
                effective_start=MEASUREMENT_START,
                effective_end=MEASUREMENT_START + timedelta(minutes=5),
                recorded_at=recorded,
                ingested_at=recorded,
                fetched_at=recorded,
                source_calendar_date=MEASUREMENT_START.date(),
                source_ref=event_id,
                timezone="UTC",
                quality="observed",
                valid=True,
                sequence=0,
                projection_version=1,
                feature_version="synthetic-event-projection",
            )
        )
    event_answer = submit(python, AGGREGATE_QUESTION + " through a saved event")["reply"]
    assert "/value: 40" in event_answer["text"] and shown(event_answer["id"]), event_answer
    with engine.begin() as connection:
        connection.execute(update(Event).where(Event.id == event_id).values(deleted=True))
    assert not shown(event_answer["id"])
    assert (
        native.post(f"/web-chat/messages/{event_answer['id']}/read", headers=HEADERS).status_code
        == 409
    )
    print("Verified historical aggregates and withheld changed measurement and event evidence")


def exercise_statistics(engine, python, native):
    def midnight(offset):
        return datetime.combine(STATISTICS_START + timedelta(days=offset), time.min, tzinfo=UTC)

    with Session(engine) as session, session.begin():
        packs = ensure_scenario_packs(session)
        for pack in ("sleep", "training", "migraine"):
            packs[pack].llm_enabled = True
            packs[pack].tracking_enabled = True
        for offset in range(14):
            session.add(
                HealthDay(
                    day=midnight(offset).date(),
                    steps=3000 + 37 * offset,
                    sleep_score=50 + 2 * offset,
                )
            )
        for event in (
            EventInput(
                start=midnight(2) + timedelta(hours=10),
                end=midnight(2) + timedelta(hours=14),
                timezone="UTC",
                payload={"type": "migraine"},
            ),
            EventInput(
                start=midnight(9),
                end=midnight(10),
                timezone="UTC",
                payload={"type": "headache_observation", "headache": "no", "migraine": "no"},
            ),
        ):
            create_event(session, event, actor="synthetic-fixture")
    old, new = [submit(client, STATISTICS_QUESTION)["reply"] for client in (python, native)]
    assert old["text"] == new["text"]
    assert new["text"] == (
        "The stored synthetic records contain matched daily observations.\n\n"
        "Based on saved Garmin and diary data."
    )
    with Session(engine) as session, session.begin():
        packs = ensure_scenario_packs(session)
        packs["training"].llm_enabled = False
        packs["migraine"].llm_enabled = False
    history = native.get("/web-chat/messages", headers=HEADERS)
    assert history.status_code == 200
    assert new["id"] not in {row["id"] for row in history.json()["replies"]}
    assert native.post(f"/web-chat/messages/{new['id']}/read", headers=HEADERS).status_code == 409
    question = STATISTICS_QUESTION + " without the migraine pack"
    denied = [submit(client, question)["reply"]["text"] for client in (python, native)]
    assert denied[0] == denied[1] and "The analysis could not be completed" in denied[1]
    print("Matched paired lag and migraine analysis and withheld sources after a pack was disabled")


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
