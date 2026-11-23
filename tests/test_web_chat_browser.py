"""Exercise the authenticated web chat through a real browser and HTTP server."""

import json
import os
import select
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect, sync_playwright
from psycopg import sql
from sqlalchemy.engine import make_url

KEY = "synthetic-browser-owner-" + "c" * 32
READ_KEY = "synthetic-browser-read-diary-" + "r" * 32
TOKENS = [
    {"key": KEY, "scopes": ["admin"]},
    {"key": READ_KEY, "scopes": ["read:diary"]},
]


@pytest.fixture
def browser_api(tmp_path):
    native = os.environ.get("GA_TEST_NATIVE_WEB")
    database_url = os.environ.get("GA_TEST_DATABASE_URL")
    if not native or not database_url:
        pytest.skip("Requires the Rust binary and a local TimescaleDB fixture")

    base = make_url(database_url)
    assert base.drivername == "postgresql+psycopg"
    assert base.host in {"127.0.0.1", "::1", "localhost"} and not base.query
    assert base.database and base.database.endswith("_test")
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    name = f"garmin_browser_{uuid4().hex}_test"
    environment = {
        "GA_DATABASE_URL": base.set(database=name).render_as_string(hide_password=False),
        "GA_API_TOKENS": json.dumps(TOKENS),
    }
    binary = Path(native).resolve(strict=True)
    with psycopg.connect(admin, autocommit=True) as connection:
        assert connection.execute("SELECT current_database()").fetchone() == (base.database,)
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            subprocess.run(
                [binary, "migrate"], cwd=tmp_path, env=environment, check=True, timeout=60
            )
            with subprocess.Popen(
                [binary, "serve", "--host", "127.0.0.1", "--port", "0"],
                cwd=tmp_path,
                env=environment,
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    assert select.select([process.stdout], [], [], 10)[0], (
                        "Native API startup timed out"
                    )
                    address = process.stdout.readline().strip().removeprefix("Listening on ")
                    url = urlsplit(address)
                    assert url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
                    yield address
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        finally:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_browser_connect_note_history_and_disconnect(browser_api):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(browser_api + "/chat")
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        page.locator("#token").fill(KEY)
        page.locator('#connect-form button[type="submit"]').click()
        expect(page.locator("#chat")).to_be_visible()

        page.locator("#message").fill("/note Synthetic browser walk")
        page.locator("#send").click()
        expect(page.locator("#messages .bubble.assistant").last).to_contain_text(
            "Заметка сохранена", timeout=10000
        )
        page.locator("#message").fill("/history")
        page.locator("#send").click()
        expect(page.locator("#messages .bubble.assistant").last).to_contain_text(
            "Последние записи:", timeout=10000
        )
        assert page.locator("#messages .bubble").evaluate_all(
            "nodes => nodes.map(node => node.classList.contains('owner') ? 'owner' : 'assistant')"
        ) == ["owner", "assistant", "owner", "assistant"]
        # Rendering can finish before the preceding refresh has acknowledged
        # its replies. Wait for that refresh before intercepting the next one.
        expect(page.locator("#send")).to_be_enabled()
        held = []

        def hold_refresh(route):
            if route.request.method == "GET":
                held.append(route)
            else:
                route.continue_()

        page.route("**/web-chat/messages", hold_refresh)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        # If the visibility event overlaps a scheduled refresh, the next
        # periodic refresh is another valid in-flight request to interrupt.
        for _ in range(120):
            if held:
                break
            page.wait_for_timeout(50)
        assert held
        page.locator("#disconnect").click()
        held[0].fulfill(
            json={
                "messages": [
                    {
                        "id": "late",
                        "text": "late private message",
                        "created_at": "2026-10-07T18:00:00Z",
                    }
                ],
                "replies": [],
            }
        )
        page.wait_for_timeout(200)
        expect(page.locator("#chat")).to_be_hidden()
        assert page.locator("#messages .bubble").count() == 0
        assert not errors, errors
        browser.close()


def test_browser_dashboard_owner_can_correct_entry_and_reader_cannot(browser_api):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(timezone_id="Europe/Bratislava")
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            response = page.goto(browser_api + "/dashboard")
            assert "connect-src 'self'" in response.headers["content-security-policy"]

            def connect(key):
                page.locator("#connect").click()
                page.locator("#token").fill(key)
                page.locator('#auth-form button[type="submit"]').click()
                expect(page.locator("#mode")).to_have_text("Подключено")

            connect(KEY)
            page.locator("#tracker-setup").locator("xpath=../summary").click()
            page.locator("#tracker-name").fill("Прогулки")
            page.locator("#tracker-key").fill("browser_walk")
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Шаги")
            field.locator('[data-field="key"]').fill("steps")
            field.locator('[data-field="kind"]').select_option("integer")
            field.locator('[data-field="unit"]').fill("steps")
            field.locator('[data-field="min"]').fill("0")
            field.locator('[data-field="max"]').fill("10000")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-preview-text")).to_contain_text("Шаги")
            page.locator("#confirm-tracker").click()
            page.get_by_role("button", name="Прогулки", exact=True).click()

            row = page.locator("#diary-rows tr").filter(has_text="Прогулки")
            value = page.locator('#entry-fields [data-name="steps"]')
            submit = page.locator('#entry-form button[type="submit"]')
            value.fill("2400")
            submit.click()
            expect(row).to_contain_text("steps: 2400")
            row.get_by_role("button", name="Исправить", exact=True).click()
            expect(value).to_have_value("2400")
            value.fill("3200")
            submit.click()
            expect(row).to_contain_text("steps: 3200")
            expect(row).to_have_count(1)

            page.locator("#connect").click()
            expect(page.locator("#mode")).to_have_text("Отключено")
            expect(row).to_have_count(0)
            connect(READ_KEY)
            expect(row).to_contain_text("steps: 3200")
            expect(row.get_by_role("button", name="Исправить", exact=True)).to_have_count(0)
            assert not errors, errors
            assert all(url.startswith(browser_api + "/") for url in requests)
        finally:
            browser.close()
