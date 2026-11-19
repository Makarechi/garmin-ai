"""Compare tracker extraction through HTTP and a synthetic local HTTPS provider.

Run in a disposable Linux test container with writable /etc/hosts and port 443.
The provider hostname resolves to a temporary loopback TLS server during the test.
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from capture_reference import isolated_engine
from check_tracker_nl import TOKENS, seed
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import AppState, Event, EventDefinition, EventDefinitionVersion, Person
from garmin_ai.natural_language import NaturalLanguageRequest, TrackerExtraction
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

INSTANCE = "model:openai:synthetic"
MODEL = "synthetic-tracker-model"
NOW = "2026-09-20T20:00:00+00:00"
INTEGRATION = {
    "id": INSTANCE,
    "kind": "model",
    "provider": "openai",
    "secret_refs": {"api_key": "GA_PLUGIN_TRACKER_SYNTHETIC"},
    "config": {"model": MODEL, "structured_output": True, "timeout_seconds": 5},
}


def extraction(prompt):
    text = prompt["text"]
    candidates = prompt["candidate_trackers"]
    assert len(candidates) <= 5
    assert all("events" not in row and "original_text" not in row for row in candidates)
    candidate = candidates[0]
    value = text.split()[-1].strip('"')
    begin = text.rindex(value)
    fields = [
        {
            "field_id": candidate["fields"][0]["field_id"],
            "value": value,
            "evidence": {"start": begin, "end": begin + len(value), "quote": value},
            "unit": None,
            "unit_evidence": None,
        }
    ]
    field = candidate["fields"][0]
    if field["semantic"] == "quantity":
        token = text.split()[-2]
        value = float(token)
        begin = text.rindex(token)
        fields[0].update(
            value=value,
            evidence={"start": begin, "end": begin + len(token), "quote": token},
            unit=field["unit"],
            unit_evidence={"start": text.rindex("minutes"), "end": len(text), "quote": "minutes"},
        )
    elif field["schema"].get("type") == "boolean":
        fields[0]["value"] = True
    result = {
        "schema_version": "tracker.nl.v1",
        "intent": "create_entry",
        "definition_version_id": candidate["definition_version_id"],
        "event_id": None,
        "start": prompt["now"],
        "end": None,
        "start_evidence": {"start": 0, "end": len(text), "quote": text},
        "end_evidence": None,
        "fields": fields,
        "tracker_draft": None,
        "clarification": None,
        "confidence": 0.99,
    }
    if prompt["selected_event"] is not None:
        result.update(
            intent="update_entry",
            event_id=prompt["selected_event"]["id"],
            start=None,
            start_evidence=None,
        )
    if "low-confidence" in text:
        result["confidence"] = 0.5
    if "none-response" in text:
        result["intent"] = "none"
    if "clarify-response" in text:
        result.update(intent="clarify", clarification="Synthetic clarification")
    if "unoffered" in text:
        result["definition_version_id"] = "00000000-0000-0000-0000-000000000042"
    if "invalid-quote" in text:
        result["fields"][0]["evidence"]["quote"] = "different synthetic quote"
    if "upper-calendar" in text:
        result["start"] = "9999-12-31T12:30:00Z"
    if "clock-only" in text:
        result.update(
            start="2026-09-20T19:03:00Z",
            start_evidence={
                "start": text.index("19:03"),
                "end": text.index("19:03") + 5,
                "quote": "19:03",
            },
        )
    if "propose-response" in text or "change-response" in text:
        result.update(
            intent="change_tracker" if "change-response" in text else "propose_tracker",
            tracker_draft={
                "key": candidate["definition_key"].removeprefix("user."),
                "name": "Synthetic proposed tracker",
                "locale": "en",
                "fields": [{"key": "value", "label": "Value", "kind": "text"}],
            },
        )
    return result


class SyntheticProvider:
    instance_id = INSTANCE

    def __init__(self, calls):
        self.calls = calls

    def structured(self, instruction, prompt, schema, *, categories=None):
        assert "candidate tracker contracts" in instruction
        self.calls.append(json.loads(prompt))
        return schema.model_validate(extraction(json.loads(prompt)))

    def close(self):
        pass


def certificates(directory, hostname="api.openai.com"):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic tracker test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    leaf = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = [
        directory / name for name in ["ca.pem", "server.pem", "server.key"]
    ]
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return ca_path, cert_path, key_path


@contextmanager
def synthetic_https(directory, calls):
    ca, certificate, key = certificates(directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/responses"
                assert self.headers["Authorization"] == "Bearer synthetic-tracker-provider-key"
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert request["model"] == MODEL and request["store"] is False
                assert request["text"]["format"]["strict"] is True
                prompt = json.loads(request["input"][1]["content"])
                calls.append(prompt)
                if "concurrent-response" in prompt["text"]:
                    time.sleep(1.6)
                result = TrackerExtraction.model_validate(extraction(prompt)).model_dump(
                    mode="json"
                )
                output_text = json.dumps(result)
                if "decimal-wire" in prompt["text"]:
                    result["fields"][0]["value"] = "SYNTHETIC_WIRE_DECIMAL"
                    output_text = json.dumps(result).replace(
                        '"SYNTHETIC_WIRE_DECIMAL"', "1.000000000000000000123"
                    )
                body = json.dumps(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": output_text}],
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                calls.append({"error": type(error).__name__})
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    hosts = Path("/etc/hosts")
    line = "127.0.0.1 api.openai.com # synthetic-garmin-tracker-extraction-test\n"
    hosts.write_text(hosts.read_text() + "\n" + line)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        assert all(
            ipaddress.ip_address(item[4][0]).is_loopback
            for item in socket.getaddrinfo("api.openai.com", 443)
        )
        thread.start()
        yield ca
    finally:
        hosts.write_text(hosts.read_text().replace(line, ""))
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()


def pinned(engine, body):
    request = NaturalLanguageRequest.model_validate(body)
    digest = sha256(request.model_dump_json(exclude_none=False).encode()).hexdigest()
    key = "nl-context:" + sha256(f"api\0{request.operation_id}".encode()).hexdigest()
    with Session(engine) as session:
        session.add(
            AppState(key=key, value={"request_hash": digest, "message_at": NOW, "timezone": "UTC"})
        )
        session.commit()


def logical_event(engine, identity):
    with Session(engine) as session:
        event = session.get(Event, identity)
        assert event is not None
        return {
            "payload": event.payload,
            "start": event.start,
            "end": event.end,
            "timezone": event.timezone,
            "source": event.source,
            "status": event.status,
            "confidence": event.confidence,
            "original_text": event.original_text,
            "evidence_refs": event.evidence_refs,
            "revision": event.revision,
        }


def clean_response(value):
    if isinstance(value, dict):
        value = {key: clean_response(item) for key, item in value.items()}
        if value.get("submission_id") is not None:
            assert re.fullmatch(r"[0-9a-f]{32}", value["submission_id"])
            value["submission_id"] = "synthetic-submission"
        if "allowed_operations" in value:
            value["allowed_operations"] = sorted(value["allowed_operations"])
        if "event_id" in value:
            value["event_id"] = "synthetic-entry" if value["event_id"] else None
        if "confirmation_token" in value:
            assert len(value["confirmation_token"]) == 64
            value["confirmation_token"] = "synthetic-preview"
    elif isinstance(value, list):
        value = [clean_response(item) for item in value]
    return value


def exercise(engine, python, native, python_calls, native_calls):
    with Session(engine) as session:
        version = str(
            session.scalar(
                select(EventDefinitionVersion.id)
                .join(EventDefinition)
                .where(EventDefinition.key == "user.synthetic_nl_001")
            )
        )
        sensitive = str(
            session.scalar(
                select(EventDefinitionVersion.id)
                .join(EventDefinition)
                .where(EventDefinition.key == "user.synthetic_nl_000")
            )
        )
    with Session(engine) as session:
        numeric = str(
            session.scalar(
                select(EventDefinitionVersion.id)
                .join(EventDefinition)
                .where(EventDefinition.key == "user.synthetic_quantity")
            )
        )
        boolean = str(
            session.scalar(
                select(EventDefinitionVersion.id)
                .join(EventDefinition)
                .where(EventDefinition.key == "user.synthetic_boolean")
            )
        )
    requests = [
        "Synthetic now blue",
        "Синтетический сейчас синий",
        "Synthetic now not blue",
        'Synthetic now "blue"',
        "Synthetic now will blue",
        "Synthetic now low-confidence blue",
        "Synthetic now none-response blue",
        "Synthetic now clarify-response blue",
        "Synthetic clock-only at 19:03 blue",
        "Synthetic now propose-response blue",
        "Synthetic now change-response blue",
        "Synthetic upper-calendar 9999-12-31 at 12:30 blue",
    ]
    requests = [(text, version) for text in requests] + [
        ("Synthetic now 15.5 minutes", numeric),
        ("Synthetic now decimal-wire 1 minutes", numeric),
        ("Synthetic now yes", boolean),
    ]
    saved = {}
    count = 0
    for index, (text, selected_version) in enumerate(requests):
        responses = []
        events = []
        for name, client in [("source", python), ("native", native)]:
            body = {
                "text": text,
                "operation_id": f"synthetic-http-{index}-{name}",
                "selected_definition_version_id": selected_version,
            }
            pinned(engine, body)
            response = client.post(
                "/natural-language/trackers",
                json=body,
                headers={"Authorization": "Bearer synthetic-tracker-nl-fallback-api-admin"},
            )
            assert response.status_code == 200, (name, index, response.status_code, response.text)
            result = response.json()
            responses.append(clean_response(result))
            count += 1
            if result["written"]:
                events.append(logical_event(engine, result["event_id"]))
                saved[(index, name)] = result["event_id"]
                before = len(python_calls), len(native_calls)
                for replayer in [python, native]:
                    repeated = replayer.post(
                        "/natural-language/trackers",
                        json=body,
                        headers={"Authorization": "Bearer synthetic-tracker-nl-fallback-api-admin"},
                    )
                    assert repeated.status_code == 200 and repeated.json() == result
                    count += 1
                assert before == (len(python_calls), len(native_calls))
        assert responses[0] == responses[1], (text, responses)
        if events:
            assert events[0] == events[1], (text, events)
    updates = []
    for name, client in [("source", python), ("native", native)]:
        body = {
            "text": "Synthetic now green",
            "operation_id": f"synthetic-http-update-{name}",
            "selected_event_id": saved[(0, name)],
            "selected_definition_version_id": version,
        }
        pinned(engine, body)
        response = client.post(
            "/natural-language/trackers",
            json=body,
            headers={"Authorization": "Bearer synthetic-tracker-nl-fallback-api-admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 2
        updates.append(logical_event(engine, saved[(0, name)]))
        count += 1
    assert updates[0] == updates[1]
    for role, selected, status in [
        ("read", version, 403),
        ("health", version, 403),
        ("admin", sensitive, 200),
    ]:
        responses = []
        for name, client in [("source", python), ("native", native)]:
            body = {
                "text": "Synthetic now blue",
                "operation_id": f"synthetic-http-scope-{role}-{name}",
                "selected_definition_version_id": selected,
            }
            response = client.post(
                "/natural-language/trackers",
                json=body,
                headers={"Authorization": f"Bearer synthetic-tracker-nl-fallback-api-{role}"},
            )
            assert response.status_code == status, (role, name, response.status_code, response.text)
            responses.append(clean_response(response.json()))
            count += 1
        if status == 200:
            assert responses[0] == responses[1]
            assert responses[0]["reason"] == "sensitive_tracker_consent_required"
    for index, text in enumerate(
        ["Synthetic now unoffered blue", "Synthetic now invalid-quote blue"]
    ):
        for name, client in [("source", python), ("native", native)]:
            body = {
                "text": text,
                "operation_id": f"synthetic-http-invalid-{index}-{name}",
                "selected_definition_version_id": version,
            }
            pinned(engine, body)
            response = client.post(
                "/natural-language/trackers",
                json=body,
                headers={"Authorization": "Bearer synthetic-tracker-nl-fallback-api-admin"},
            )
            assert response.status_code == 422, (name, text, response.status_code, response.text)
            count += 1
    assert len(native_calls) == len(requests) + 4 and all(
        "error" not in item for item in native_calls
    )
    assert len(python_calls) == len(native_calls), (len(python_calls), len(native_calls))
    body = {
        "text": "Synthetic now concurrent-response purple",
        "operation_id": "synthetic-http-concurrent",
        "selected_definition_version_id": version,
    }
    pinned(engine, body)
    headers = {"Authorization": "Bearer synthetic-tracker-nl-fallback-api-admin"}
    before = len(native_calls)
    with ThreadPoolExecutor(max_workers=3) as executor:
        first_request = executor.submit(
            native.post, "/natural-language/trackers", json=body, headers=headers
        )
        until = time.monotonic() + 5
        while len(native_calls) == before and time.monotonic() < until:
            time.sleep(0.01)
        assert len(native_calls) == before + 1 and not first_request.done()
        second_request = executor.submit(
            native.post, "/natural-language/trackers", json=body, headers=headers
        )
        read_started = time.monotonic()
        messages = native.get("/web-chat/messages", headers=headers)
        read_elapsed = time.monotonic() - read_started
        overlapped = not first_request.done()
        first, second = first_request.result(), second_request.result()
    print(
        f"Slow-provider overlap returned tracker {first.status_code}/{second.status_code}, "
        f"web chat {messages.status_code}, read {read_elapsed:.3f}s, overlapped {overlapped}",
        flush=True,
    )
    assert messages.status_code == 200 and read_elapsed < 1.0 and overlapped, messages.text
    assert first.status_code == 200 and second.status_code == 200, (
        first.status_code,
        first.text,
        second.status_code,
        second.text,
    )
    assert first.json() == second.json() and first.json()["written"] is True
    assert len(native_calls) == before + 1
    repeated = python.post("/natural-language/trackers", json=body, headers=headers)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    assert len(python_calls) == len(requests) + 4
    count += 4
    print(
        f"Matched {count} actual HTTP requests with {len(native_calls)} native HTTPS provider calls and {len(python_calls)} frozen provider calls",
        flush=True,
    )
    print(
        "Compared native/source creation, corrections, quoted/negated/future values, temporal clarification, proposals, scopes, sensitive consent, provenance and cross-runtime receipts",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    seed(engine, 0, 3)
    with Session(engine) as session:
        person = session.scalar(select(Person).where(Person.singleton))
        person.locale, person.timezone = "en", "UTC"
        session.commit()
    with Session(engine) as session:
        for name, kind, unit in [("quantity", "number", "minutes"), ("boolean", "boolean", None)]:
            draft = TrackerSetupDraft(
                key=f"synthetic_{name}",
                name=f"Synthetic {name}",
                locale="en",
                fields=[
                    TrackerFieldDraft(
                        key="value",
                        label="Synthetic value",
                        kind=kind,
                        unit=unit,
                        minimum=0 if kind == "number" else None,
                        maximum=240 if kind == "number" else None,
                    )
                ],
            )
            preview = preview_tracker(session, draft)
            confirm_tracker(
                session,
                TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
                actor="synthetic-fixture",
            )
        session.commit()
    settings = Settings(
        _env_file=None,
        locale="en",
        timezone="UTC",
        llm_enabled=True,
        integrations=[INTEGRATION],
        api_tokens=[ApiToken(**token) for token in TOKENS],
    )
    python_calls, native_calls = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="synthetic-tracker-extraction-") as path:
            directory = Path(path)
            with (
                synthetic_https(directory, native_calls) as ca,
                patch(
                    "garmin_ai.integrations.create_model_provider",
                    lambda *_args, **_kwargs: SyntheticProvider(python_calls),
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
                        "GA_PLUGIN_TRACKER_SYNTHETIC": "synthetic-tracker-provider-key",
                        "GA_LLM_CONSENT": json.dumps(
                            {
                                "provider": "openai",
                                "provider_instance_id": INSTANCE,
                                "model": MODEL,
                                "categories": ["diary"],
                                "policy_revision": 1,
                                "granted_at": "2026-09-20T19:00:00+00:00",
                            }
                        ),
                        "SSL_CERT_FILE": str(ca),
                    },
                    stdout=subprocess.PIPE,
                    text=True,
                ) as server,
            ):
                try:
                    url = server.stdout.readline().strip().removeprefix("Listening on ")
                    assert url.startswith("http://127.0.0.1:")
                    with httpx.Client(base_url=url, timeout=15, trust_env=False) as native:
                        exercise(engine, python, native, python_calls, native_calls)
                finally:
                    status = Path(f"/proc/{server.pid}/status")
                    if status.exists():
                        for line in status.read_text().splitlines():
                            if line.startswith(("VmHWM:", "VmRSS:")):
                                print(f"Native model HTTP process {line}", flush=True)
                    server.terminate()
                    server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
