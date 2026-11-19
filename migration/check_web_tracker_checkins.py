"""Compare owner web tracker reminders from the normal native worker with Python."""

import argparse
import json
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from check_tracker_checkins import add_tracker
from check_web_chat_model import HEADERS, TOKENS, set_model_categories, submit
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.definitions import create_custom_event
from garmin_ai.initiative_rules import queue_due_tracker_checkins

ROUTE = "/web-chat/messages"
BUDGET = 3
SETTINGS = Settings(
    _env_file=None,
    api_tokens=TOKENS,
    timezone="UTC",
    locale="en",
    quiet_start_hour=0,
    quiet_end_hour=0,
    question_budget=BUDGET,
)


@contextmanager
def running(binary, mode, env, directory):
    with subprocess.Popen(
        [binary, *mode], env=env, cwd=directory, stdout=subprocess.PIPE, text=True
    ) as process:
        try:
            output = process.stdout.readline().strip()
            assert output and process.poll() is None, (mode, output)
            yield output
        finally:
            process.terminate()
            process.wait(timeout=5)


def seed(engine, clock):
    with Session(engine) as session, session.begin():
        person = owner(session)
        person.locale = "en"
        person.timezone = "UTC"
        ready = add_tracker(session, "web_ready", clock=clock)
        stale = add_tracker(session, "web_stale", clock=clock)
        add_tracker(session, "web_sensitive", clock=clock, privacy="sensitive")
        session.execute(text("UPDATE tracker_configs SET reminder_timezone='UTC'"))
    return ready["tracker"], stale["tracker"]


def rows(engine):
    with engine.connect() as connection:
        return {
            row.dedup_key: (str(row.id), row.state, row.intent["blocks"][0]["text"])
            for row in connection.execute(
                text(
                    "SELECT id,state,dedup_key,intent FROM outbox_messages "
                    "WHERE starts_with(dedup_key,'rule:')"
                )
            )
        }


def wait_rows(engine, count):
    deadline = time.monotonic() + 20
    while True:
        found = rows(engine)
        if len(found) >= count or time.monotonic() >= deadline:
            assert len(found) == count, found
            return found
        time.sleep(0.05)


def history(client, ids):
    response = client.get(ROUTE, headers=HEADERS)
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["replies"] if item["id"] in ids}


def browser(url, identity, reminder):
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            page = chromium.new_page(viewport={"width": 1180, "height": 900})
            page.goto(url + "/chat")
            page.locator("#token").fill(TOKENS[0]["key"])
            with page.expect_response(
                lambda response: (
                    response.url.endswith(f"/{identity}/read") and response.request.method == "POST"
                )
            ) as acknowledged:
                page.locator('#connect-form button[type="submit"]').click()
                expect(page.locator(".bubble.assistant").filter(has_text=reminder)).to_be_visible()
            assert acknowledged.value.ok
            expect(page.locator("#connection-status")).to_contain_text("Подключено")
            page.locator("#disconnect").click()
            expect(page.locator("#chat")).to_be_hidden()
        finally:
            chromium.close()


def exercise(engine, python, native, url, now, ready, stale, expected):
    assert native.get(ROUTE).status_code == 401
    actual = wait_rows(engine, 2)
    assert {key: row[2] for key, row in actual.items()} == expected
    assert {row[1] for row in actual.values()} == {"queued"}
    by_text = {row[2]: row[0] for row in actual.values()}
    ready_id = by_text[f"Reminder: {ready['shortcut']}."]
    stale_id = by_text[f"Reminder: {stale['shortcut']}."]
    both = {ready_id, stale_id}
    assert history(native, both) == history(python, both)
    assert set(history(native, both)) == both
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM message_delivery_receipts")) == 0

    with Session(engine) as session, session.begin():
        create_custom_event(
            session,
            {
                "definition_key": stale["definition_key"],
                "start": now - timedelta(minutes=3),
                "timezone": "UTC",
                "values": {"value": 7},
            },
            actor="synthetic-fixture",
        )
    assert set(history(native, both)) == {ready_id}
    assert (
        rows(engine)[next(key for key, row in actual.items() if row[0] == stale_id)][1]
        == "cancelled"
    )
    rejected = native.post(f"{ROUTE}/{stale_id}/read", headers=HEADERS)
    assert rejected.status_code == 409, rejected.text
    assert set(history(python, both)) == {ready_id}

    browser(url, ready_id, f"Reminder: {ready['shortcut']}.")
    assert native.post(f"{ROUTE}/{ready_id}/read", headers=HEADERS).status_code == 200
    assert history(native, both)[ready_id]["state"] == "read"
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT outbox_message_id::text,state FROM message_delivery_receipts "
                "WHERE outbox_message_id::text IN (:ready,:stale)"
            ),
            {"ready": ready_id, "stale": stale_id},
        ).all() == [(ready_id, "read")]
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    now = datetime.now(UTC)
    clock = (now - timedelta(minutes=3)).strftime("%H:%M")
    try:
        set_model_categories(engine, [])
        ready, stale = seed(engine, clock)
        with TestClient(create_app(SETTINGS, engine)) as python:
            submit(python, "/help")
            with Session(engine) as session:
                expected = {
                    row.dedup_key: row.intent["blocks"][0]["text"]
                    for row in queue_due_tracker_checkins(session, SETTINGS, now)
                }
                assert len(expected) == 2
                session.rollback()
            assert not rows(engine)
            with TemporaryDirectory(prefix="synthetic-web-checkins-") as directory:
                root = Path(directory)
                (root / "data" / "raw").mkdir(mode=0o700, parents=True)
                env = {
                    "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_API_TOKENS": json.dumps(TOKENS),
                    "GA_DATA_DIR": str(root / "data"),
                    "GA_LOCALE": "en",
                    "GA_TIMEZONE": "UTC",
                    "GA_QUIET_START_HOUR": "0",
                    "GA_QUIET_END_HOUR": "0",
                    "GA_QUESTION_BUDGET": str(BUDGET),
                }
                with running(binary, ["serve", "--port", "0"], env, root) as ready_line:
                    url = ready_line.removeprefix("Listening on ")
                    assert url.startswith("http://127.0.0.1:")
                    with httpx.Client(base_url=url, timeout=6, trust_env=False) as native:
                        with running(binary, ["worker"], env, root):
                            first = exercise(
                                engine, python, native, url, now, ready, stale, expected
                            )
                        with Session(engine) as session, session.begin():
                            added = add_tracker(session, "web_pause", clock=clock)
                            session.execute(
                                text("UPDATE tracker_configs SET reminder_timezone='UTC'")
                            )
                        with running(binary, ["worker"], env, root):
                            second = wait_rows(engine, 3)
                            assert all(second[key][0] == item[0] for key, item in first.items())
                            queued = next(item for key, item in second.items() if key not in first)
                            assert queued[2] == f"Reminder: {added['tracker']['shortcut']}."
                            assert queued[0] in history(native, {queued[0]})
                            submit(native, "/pause")
                            assert not history(native, {queued[0]})
                            assert (
                                native.post(
                                    f"{ROUTE}/{queued[0]}/read", headers=HEADERS
                                ).status_code
                                == 409
                            )
                            assert {item[1] for item in rows(engine).values()} == {
                                "read",
                                "cancelled",
                            }
    finally:
        engine.dispose()
    print(
        "Matched Python web check-ins and verified browser display, one receipt, entry edits, restart and pause"
    )


if __name__ == "__main__":
    main()
