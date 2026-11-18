"""Compare web chat retrieval and browser read acknowledgements with the pinned Python server."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import Settings

ROUTE = "/web-chat/messages"
KEY = "synthetic-browser-fixture-owner-read-key"
SCOPED = "synthetic-browser-fixture-limited-key"
API_TOKENS = [{"key": KEY, "scopes": ["admin"]}, {"key": SCOPED, "scopes": ["read:diary"]}]
HEADERS = {"Authorization": f"Bearer {KEY}"}
LIMITED = {"Authorization": f"Bearer {SCOPED}"}


def seed(engine, python):
    identities = []
    for text_value in ("/note Synthetic web chat transfer", "/help"):
        reply = python.post(
            ROUTE,
            json={"client_message_id": str(uuid4()), "text": text_value},
            headers=HEADERS,
        )
        assert reply.status_code == 200, reply.text
        identities.append(reply.json()["reply"]["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_messages SET intent=jsonb_set(intent,'{blocks}',"
                " intent->'blocks' || CAST(:extra AS jsonb)) WHERE id=CAST(:id AS uuid)"
            ),
            {
                "id": identities[-1],
                "extra": json.dumps([{"text": 'Quotes: "abc"\\ and новая строка\n'}]),
            },
        )
    body = python.get(ROUTE, headers=HEADERS).json()
    assert len(body["messages"]) == len(body["replies"]) == 2
    assert all(reply["state"] == "queued" for reply in body["replies"])
    return body


def verify_receipts(engine, expected):
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT r.outbox_message_id,r.state,r.observed_at "
                "FROM message_delivery_receipts r ORDER BY r.outbox_message_id"
            )
        ).all()
        assert len(rows) == len(expected)
        assert {str(row[0]) for row in rows} == set(expected)
        assert all(row[1] == "read" and row[2] is not None for row in rows)
        legacy = connection.execute(
            text("SELECT count(*) FROM app_state WHERE starts_with(key,'outbox:web:local:update:')")
        ).scalar_one()
        assert legacy == len(expected)


def browser(base):
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            page = chromium.new_page(viewport={"width": 1180, "height": 900})
            receipts = []
            painted = []
            page.on(
                "response",
                lambda response: (
                    receipts.append(response)
                    if response.request.method == "POST" and response.url.endswith("/read")
                    else None
                ),
            )

            def observe_read(route):
                painted.append(page.locator("#messages .bubble.assistant").count())
                route.continue_()

            page.route("**/web-chat/messages/*/read", observe_read)
            page.goto(base + "/chat")
            page.locator("#token").fill(KEY)
            page.locator('#connect-form button[type="submit"]').click()
            expect(page.locator("#messages .bubble.owner").first).to_contain_text(
                "Synthetic web chat transfer"
            )
            expect(page.locator("#messages .bubble.assistant").first).to_contain_text(
                "Заметка сохранена"
            )
            expect(page.locator("#connection-status")).to_contain_text("Подключено")
            assert len(receipts) == 2 and all(reply.ok for reply in receipts)
            assert painted == [2, 2]
            page.locator("#disconnect").click()
            expect(page.locator("#chat")).to_be_hidden()
        finally:
            chromium.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TestClient(create_app(Settings(api_tokens=API_TOKENS), engine=engine)) as python:
            expected = seed(engine, python)
            with subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(API_TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    line = process.stdout.readline().strip()
                    assert line.startswith("Listening on http://127.0.0.1:"), line
                    base = line.removeprefix("Listening on ")
                    with httpx.Client(base_url=base, timeout=5, trust_env=False) as native:
                        for request_headers, status in (({}, 401), (LIMITED, 403), (HEADERS, 200)):
                            before = python.get(ROUTE, headers=request_headers)
                            after = native.get(ROUTE, headers=request_headers)
                            assert before.status_code == after.status_code == status
                            assert before.json() == after.json()
                        verify_receipts(engine, ())
                        missing = f"{ROUTE}/{uuid4()}/read"
                        before = python.post(missing, headers=HEADERS)
                        after = native.post(missing, headers=HEADERS)
                        assert before.status_code == after.status_code == 404
                        assert before.json() == after.json()
                        browser(base)
                        replies = [reply["id"] for reply in expected["replies"]]
                        for reply in replies:
                            path = f"{ROUTE}/{reply}/read"
                            assert native.post(path, headers=HEADERS).json() == {"status": "read"}
                        assert (
                            native.get(ROUTE, headers=HEADERS).json()
                            == python.get(ROUTE, headers=HEADERS).json()
                        )
                        verify_receipts(engine, replies)
                    print(
                        "Web chat owner access, pinned history, and browser read receipts matched"
                    )
                finally:
                    process.terminate()
                    process.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
