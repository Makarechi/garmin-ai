"""Compare JSON input validation before typed HTTP request handlers."""

import argparse
import json
import os
import subprocess
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings

TOKEN = {
    "key": "synthetic-json-request-validation-proof",
    "scopes": ["read:health", "read:diary", "write:diary", "manage:integrations"],
}
JSON = "application/json"
CASES = (
    ("empty", b"", JSON),
    ("null", b"null", JSON),
    ("whitespace", b" \t\r\n", JSON),
    ("object-eof", b"{", JSON),
    ("array-eof", b"[", JSON),
    ("value-eof", b'{"arguments":', JSON),
    ("missing-property-quotes", b"{foo:42}", JSON),
    ("object-trailing-comma", b'{"arguments":{},}', JSON),
    ("array-trailing-comma", b"[1,]", JSON),
    ("object-missing-comma", b'{"a":1 "b":2}', JSON),
    ("array-missing-comma", b"[1 2]", JSON),
    ("missing-colon", b'{"arguments" {}}', JSON),
    ("bad-literal", b'{"a": tru}', JSON),
    ("missing-fraction", b'{"a": 1.}', JSON),
    ("missing-exponent", b'{"a": 1e+}', JSON),
    ("leading-zero", b'{"a": 001}', JSON),
    ("extra-data", b"{} []", JSON),
    ("unterminated-string", b'{"a": "line', JSON),
    ("control-character", b'{"a": "first\nsecond"}', JSON),
    ("invalid-escape", rb'{"a": "\q"}', JSON),
    ("invalid-unicode-escape", rb'{"a": "\u123g"}', JSON),
    ("multiline-codepoint-offset", '{"я": "ü",\n "☕" 3}'.encode(), JSON),
    ("bom", "\ufeff{foo}".encode(), JSON),
    ("utf16", '{"a" 5}'.encode("utf-16"), JSON),
    ("utf32", '{"a" 5}'.encode("utf-32"), JSON),
    ("nonfinite", b'{"a": Infinity}', JSON),
    ("invalid-utf8", b'{"a": "\xff"}', JSON),
    ("plain", b"{}", "text/plain"),
    ("binary-plain", b"\xff", "text/plain"),
    ("array-comma-eof", b"[1,", JSON),
    ("object-comma-eof", b'{"a":1,', JSON),
    ("slash-eof", b'{"a":"x\\', JSON),
    ("utf8-plain", "☕".encode(), "text/plain; charset=utf-8"),
    ("application-suffix", b'{"a" 1}', "application/example+json"),
    ("omitted-content-type", b'{"a" 1}', None),
)


def value(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def compare(python, rust):
    failures = []
    routes = (
        ("POST", "/tools/health_snapshot"),
        ("POST", "/hypotheses"),
        ("PUT", "/preferences/goals"),
        ("PUT", "/scenario-packs/sleep_recovery"),
    )
    for method, route in routes:
        for name, data, content_type in CASES:
            headers = {"Authorization": "Bearer " + TOKEN["key"]}
            if content_type is not None:
                headers["Content-Type"] = content_type
            expected, actual = (
                client.request(method, route, content=data, headers=headers)
                for client in (python, rust)
            )
            if expected.status_code != actual.status_code or value(expected) != value(actual):
                failures.append(
                    (
                        route,
                        name,
                        expected.status_code,
                        value(expected),
                        actual.status_code,
                        value(actual),
                    )
                )
    for item in failures:
        print("JSON validation difference", json.dumps(item, ensure_ascii=True), flush=True)
    assert not failures, len(failures)
    print("Matched raw typed HTTP request cases", len(routes) * len(CASES), flush=True)


def run(binary):
    database = isolated_engine()
    settings = Settings(_env_file=None, api_tokens=[ApiToken(**TOKEN)])
    try:
        with (
            TestClient(create_app(settings, database), raise_server_exceptions=False) as python,
            subprocess.Popen(
                [binary, "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps([TOKEN]),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process,
        ):
            try:
                url = process.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=120, trust_env=False) as rust:
                    compare(python, rust)
            finally:
                process.terminate()
                process.wait(timeout=5)
    finally:
        database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
