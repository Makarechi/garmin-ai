"""Compare deterministic web chat writes and retry a lost response in Chromium."""

import argparse
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import Settings

KEY = "synthetic-browser-owner-local-write-fixture"
AUTH = {"Authorization": f"Bearer {KEY}"}
ROUTE = "/web-chat/messages"


def without_time(value):
    return re.sub(r"\d\d\.\d\d \d\d:\d\d", "<time>", value)


def compare(python, native):
    for message in (
        "/help",
        "/note Synthetic comparison diary note",
        "/history",
        "/pause",
        "/resume",
        "/cancel",
        "I can't breathe",
        "Synthetic request without a configured model",
    ):
        payload = {"client_message_id": str(uuid4()), "text": message}
        original = python.post(ROUTE, json=payload, headers=AUTH)
        candidate = native.post(ROUTE, json=payload, headers=AUTH)
        assert original.status_code == candidate.status_code == 200, candidate.text
        left, right = original.json(), candidate.json()
        for result in (left, right):
            assert not result["duplicate"]
            assert result["reply"]["state"] == "queued"
            assert result["reply"]["inbound_message_id"] == result["message_id"]
        assert without_time(left["reply"]["text"]) == without_time(right["reply"]["text"]), message
        if message.startswith("/note "):
            retry = native.post(ROUTE, json=payload, headers=AUTH).json()
            assert retry == {**right, "duplicate": True}
            changed = {**payload, "text": "changed synthetic note"}
            first = python.post(ROUTE, json=changed, headers=AUTH)
            second = native.post(ROUTE, json=changed, headers=AUTH)
            assert first.status_code == second.status_code == 409
            assert first.json() == second.json()


def browser(base):
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            page = chromium.new_page(viewport={"width": 1180, "height": 900})
            writes = []

            def lose_first_write(route):
                if route.request.method != "POST":
                    route.continue_()
                    return
                writes.append(route.request.post_data_json)
                if len(writes) == 1:
                    result = route.fetch()
                    assert result.ok and not result.json()["duplicate"]
                    route.abort("failed")
                else:
                    route.continue_()

            page.route("**/web-chat/messages", lose_first_write)
            page.goto(base + "/chat")
            page.locator("#token").fill(KEY)
            page.locator('#connect-form button[type="submit"]').click()
            expect(page.locator("#connection-status")).to_contain_text("Подключено")
            page.locator("#message").fill("/note Synthetic browser retry")
            page.locator("#send").click()
            expect(page.locator("#send-status")).to_contain_text("Повторная отправка")
            page.locator("#send").click()
            expect(page.locator("#messages .bubble.assistant").last).to_contain_text(
                "Заметка сохранена"
            )
            assert len(writes) == 2 and writes[0] == writes[1]
            page.locator("#message").fill("/history")
            page.locator("#send").click()
            expect(page.locator("#messages .bubble.assistant").last).to_contain_text(
                "Последние записи:"
            )
        finally:
            chromium.close()


def check_storage(engine):
    with engine.connect() as session:
        data = session.execute(
            text(
                "SELECT (SELECT count(*) FROM events WHERE kind='note'),"
                "(SELECT count(*) FROM events WHERE payload->>'description'='Synthetic browser retry'),"
                "(SELECT count(*) FROM inbound_messages WHERE normalized_text='/note Synthetic browser retry'),"
                "(SELECT count(*) FROM audit_log WHERE actor='web:local' AND action='create')"
            )
        ).one()
        assert tuple(data) == (2, 1, 1, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    rust_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_url == os.environ["GA_TEST_DATABASE_URL"]:
        raise ValueError("Use distinct disposable database names")
    left = isolated_engine()
    with patch.dict(os.environ, GA_TEST_DATABASE_URL=rust_url):
        right = isolated_engine()
    try:
        with TestClient(create_app(Settings(api_key=KEY), engine=left)) as python:
            with subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                env={"GA_DATABASE_URL": rust_url, "GA_API_KEY": KEY},
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    line = process.stdout.readline().strip()
                    assert line.startswith("Listening on http://127.0.0.1:"), line
                    base = line.removeprefix("Listening on ")
                    with httpx.Client(base_url=base, timeout=5, trust_env=False) as native:
                        compare(python, native)
                    browser(base)
                    check_storage(right)
                    print(
                        "Matched local web chat replies and retried one browser note without a duplicate"
                    )
                finally:
                    process.terminate()
                    process.wait(timeout=5)
    finally:
        left.dispose()
        right.dispose()


if __name__ == "__main__":
    main()
