"""Exercise the public demo in a browser without an API, account, or secrets."""

import json
import re
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from subprocess import PIPE, Popen
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/serve_demo.py"


@contextmanager
def demo_server():
    process = Popen(
        [sys.executable, str(SCRIPT), "--port", "0"],
        stdout=PIPE,
        stderr=PIPE,
        text=True,
    )
    url = process.stdout.readline().strip().removeprefix("Open ")
    assert url.startswith("http://127.0.0.1:"), url
    requests = []
    try:
        yield url, requests
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        requests.extend(re.findall(r'"GET (\S+) HTTP/', stderr))


def test_demo_create_correct_analyze_and_reset_in_browser():
    with demo_server() as (url, requests), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            response = page.goto(url)
            assert "connect-src 'none'" in response.headers["content-security-policy"]
            expect(page.locator("#connect")).to_be_hidden()
            expect(page.locator("#mode")).to_have_text("Демонстрационные данные")
            expect(page.locator("#demo-analysis-text")).to_contain_text("медиана 3.0")
            expect(page.locator("#diary-rows tr").first).to_contain_text("Заметка")
            page.locator("#tracker-setup").locator("xpath=../summary").click()
            page.locator("#tracker-name").fill("Прогулки")
            page.locator("#tracker-key").fill("walks")
            first = page.locator(".tracker-field").first
            first.locator('[data-field="label"]').fill("Шаги")
            first.locator('[data-field="key"]').fill("steps")
            first.locator('[data-field="kind"]').select_option("integer")
            first.locator('[data-field="unit"]').fill("steps")
            first.locator('[data-field="min"]').fill("0")
            first.locator('[data-field="max"]').fill("10000")
            page.locator("#add-tracker-field").click()
            second = page.locator(".tracker-field").last
            second.locator('[data-field="label"]').fill("Самочувствие")
            second.locator('[data-field="key"]').fill("feeling")
            second.locator('[data-field="kind"]').select_option("choice")
            second.locator('[data-field="options"]').fill("хорошо, устал")
            second.locator('[data-field="options"]').fill(" , , ")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("Добавьте разные варианты")
            second.locator('[data-field="options"]').fill("хорошо, устал")
            first.locator('[data-field="key"]').fill("type")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("зарезервирован")
            first.locator('[data-field="key"]').fill("steps")
            first.locator('[data-field="unit"]').fill("calories")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("не поддерживается")
            first.locator('[data-field="unit"]').fill("steps")
            first.locator('[data-field="max"]').fill("")
            assert first.locator('[data-field="max"]').evaluate(
                "(input) => input.validity.valueMissing"
            )
            first.locator('[data-field="max"]').fill("10000")
            first.locator('[data-field="min"]').fill("11000")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("Проверьте границы")
            first.locator('[data-field="min"]').fill("0")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-preview-text")).to_contain_text("Шаги, Самочувствие")
            expect(page.locator("#tracker-preview-privacy")).to_contain_text(
                "только в памяти вкладки"
            )
            page.locator("#confirm-tracker").click()
            page.get_by_role("button", name="Прогулки").click()
            page.locator('#entry-fields [data-name="steps"]').fill("2400")
            page.locator('#entry-fields [data-name="feeling"]').select_option("хорошо")
            page.locator('#entry-form button[type="submit"]').click()
            expect(page.locator("#diary-rows")).to_contain_text("steps: 2400")
            expect(page.locator("#demo-analysis-text")).to_contain_text("среднее 2400.0 steps")
            expect(page.locator("#demo-analysis-text")).to_contain_text(
                "Самочувствие: хорошо 1, устал 0"
            )
            page.locator("#diary-rows tr").filter(has_text="steps: 2400").get_by_role(
                "button", name="Исправить"
            ).click()
            page.locator('#entry-fields [data-name="steps"]').fill("3200")
            page.locator('#entry-form button[type="submit"]').click()
            expect(page.locator("#diary-rows")).to_contain_text("steps: 3200")
            expect(page.locator("#diary-rows")).not_to_contain_text("steps: 2400")
            expect(page.locator("#demo-analysis-text")).to_contain_text("среднее 3200.0 steps")
            page.locator("#demo-reset").click()
            expect(page.locator("#tracker-actions")).not_to_contain_text("Прогулки")
            expect(page.locator("#diary-rows")).not_to_contain_text("steps: 3200")
            expect(page.locator("#demo-analysis-text")).to_contain_text("медиана 3.0")

            page.locator("#tracker-name").fill("Ночной отдых")
            page.locator("#tracker-key").fill("night_rest")
            page.locator("#tracker-topology").select_option("bounded_interval")
            page.locator("#tracker-derived-duration").check()
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Энергия")
            field.locator('[data-field="key"]').fill("energy")
            field.locator('[data-field="kind"]').select_option("integer")
            field.locator('[data-field="min"]').fill("0")
            field.locator('[data-field="max"]').fill("5")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-preview-text")).to_contain_text("Энергия")
            page.locator("#confirm-tracker").click()
            page.get_by_role("button", name="Ночной отдых").click()
            page.locator('#entry-fields [data-name="energy"]').fill("3")
            today = page.evaluate("new Date().toISOString().slice(0, 10)")
            yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
            page.locator("#entry-start").fill(f"{yesterday}T23:00")
            page.locator("#entry-end").fill(f"{yesterday}T23:00")
            page.locator('#entry-form button[type="submit"]').click()
            expect(page.locator("#entry-status")).to_contain_text("позже начала")
            page.locator("#entry-end").fill(f"{today}T01:00")
            page.locator('#entry-form button[type="submit"]').click()
            assert not page.locator("#entry-dialog").is_visible(), page.locator(
                "#entry-status"
            ).inner_text()
            page.locator("#start").fill(today)
            page.locator("#end").fill(today)
            page.locator('#range button[type="submit"]').click()
            expect(page.locator("#diary-rows")).to_contain_text("Энергия: 3")
            expect(page.locator("#demo-analysis-text")).to_contain_text("сумма 120.0 мин")
            with page.expect_download() as download_info:
                page.locator("#export").click()
            exported = json.loads(download_info.value.path().read_text())
            assert any(row["kind"] == "user.night_rest" for row in exported["rows"])
            page.locator("#demo-reset").click()
            expect(page.locator("#diary-rows")).not_to_contain_text("Энергия: 3")

            page.locator("#tracker-name").fill("Настроение")
            page.locator("#tracker-key").fill("mood")
            page.locator("#tracker-topology").select_option("open_interval")
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Оценка")
            field.locator('[data-field="key"]').fill("rating")
            field.locator('[data-field="kind"]').select_option("scale")
            field.locator('[data-field="min"]').fill("1")
            field.locator('[data-field="max"]').fill("5")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-preview-text")).to_contain_text("Оценка")
            page.locator("#confirm-tracker").click()
            for value, hour in [(1, 8), (1, 9), (5, 10)]:
                page.get_by_role("button", name="Настроение").click()
                page.locator('#entry-fields [data-name="rating"]').fill(str(value))
                page.locator("#entry-start").fill(f"{today}T{hour:02}:00")
                if hour == 8:
                    page.locator("#entry-end").fill(f"{today}T08:30")
                page.locator('#entry-form button[type="submit"]').click()
                expect(page.locator("#entry-dialog")).to_be_hidden()
            expect(page.locator("#demo-analysis-text")).to_contain_text("медиана 1.0")
            expect(page.locator("#demo-analysis-text")).to_contain_text("score_1-5")
            expect(page.locator("#diary-rows")).to_contain_text("конец не указан")
            with page.expect_download() as download_info:
                page.locator("#export").click()
            exported = json.loads(download_info.value.path().read_text())
            mood = sorted(
                (row for row in exported["rows"] if row["kind"] == "user.mood"),
                key=lambda row: row["start"],
            )
            assert mood[0]["topology"] == "bounded_interval" and mood[0]["end"]
            assert mood[1]["topology"] == "open_interval" and mood[1]["end"] is None
            assert mood[1]["missing_end"] is True
            page.locator("#start").fill(today)
            page.locator("#end").fill(yesterday)
            page.locator('#range button[type="submit"]').click()
            expect(page.locator("#mode")).to_have_text("Проверьте период")
            expect(page.locator("#demo-analysis-text")).to_be_empty()
            page.locator("#end").fill(today)
            page.locator('#range button[type="submit"]').click()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.reload()
            expect(page.locator("#diary-rows")).not_to_contain_text("steps: 3200")
            assert not errors, errors
        finally:
            browser.close()
    assert set(requests) <= {
        "/dashboard",
        "/dashboard-assets/app.js",
        "/dashboard-assets/styles.css",
        "/favicon.ico",
    }, requests


def test_demo_text_form_keeps_default_length_limit():
    with demo_server() as (url, _requests), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            page.locator("#tracker-setup").locator("xpath=../summary").click()
            page.locator("#tracker-name").fill("Заметки")
            page.locator("#tracker-key").fill("notes_demo")
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Подпись")
            field.locator('[data-field="key"]').fill("note")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-preview-text")).to_contain_text("Подпись")
            page.locator("#confirm-tracker").click()
            page.get_by_role("button", name="Заметки").click()
            assert (
                page.locator('#entry-fields [data-name="note"]').evaluate(
                    "(input) => input.maxLength"
                )
                == 500
            )
        finally:
            browser.close()


def test_demo_preview_rejects_blank_labels_and_overlong_field_ids():
    with demo_server() as (url, _requests), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            page.locator("#tracker-setup").locator("xpath=../summary").click()
            page.locator("#tracker-name").fill("   ")
            page.locator("#tracker-key").fill("a")
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Значение")
            field.locator('[data-field="key"]').fill("b")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("не могут состоять из пробелов")
            page.locator("#tracker-name").fill("Трекер")
            field.locator('[data-field="label"]').fill("   ")
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("не могут состоять из пробелов")
            field.locator('[data-field="label"]').fill("Значение")
            page.locator("#tracker-key").fill("a" * 63)
            field.locator('[data-field="key"]').fill("b" * 63)
            page.locator('#tracker-setup button[type="submit"]').click()
            expect(page.locator("#tracker-status")).to_contain_text("идентификатор слишком длинный")
        finally:
            browser.close()


def test_demo_categorical_only_tracker_has_boolean_summary_and_implicit_unit():
    with demo_server() as (url, _requests), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            page.locator("#tracker-setup").locator("xpath=../summary").click()
            page.locator("#tracker-name").fill("Привычка")
            page.locator("#tracker-key").fill("habit")
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Сделано")
            field.locator('[data-field="key"]').fill("done")
            field.locator('[data-field="kind"]').select_option("boolean")
            page.locator('#tracker-setup button[type="submit"]').click()
            page.locator("#confirm-tracker").click()
            page.get_by_role("button", name="Привычка").click()
            page.locator('#entry-fields [data-name="done"]').select_option("true")
            page.locator('#entry-form button[type="submit"]').click()
            expect(page.locator("#demo-analysis-text")).to_contain_text(
                "Сделано: да 1 из 1, доля 100%"
            )

            page.locator("#demo-reset").click()
            page.locator("#tracker-name").fill("Повторы")
            page.locator("#tracker-key").fill("repetitions")
            field = page.locator(".tracker-field").first
            field.locator('[data-field="label"]').fill("Раз")
            field.locator('[data-field="key"]').fill("times")
            field.locator('[data-field="kind"]').select_option("integer")
            field.locator('[data-field="min"]').fill("0")
            field.locator('[data-field="max"]').fill("10")
            page.locator('#tracker-setup button[type="submit"]').click()
            page.locator("#confirm-tracker").click()
            page.get_by_role("button", name="Повторы").click()
            expect(page.locator("#entry-fields label")).to_contain_text("Раз (count)")
            page.locator('#entry-fields [data-name="times"]').fill("3")
            page.locator('#entry-form button[type="submit"]').click()
            expect(page.locator("#demo-analysis-text")).to_contain_text("среднее 3.0 count")
        finally:
            browser.close()


def test_authenticated_diary_shows_edit_only_with_write_permission_and_update_contract():
    with demo_server() as (url, _requests), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            permissions = {"write": False, "update": True}
            event_day = datetime.now(UTC).date().isoformat()

            def serve_page(route):
                response = route.fetch()
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() != "content-security-policy"
                }
                route.fulfill(response=response, headers=headers)

            def serve_api(route):
                path = urlsplit(route.request.url).path
                if path == "/capabilities":
                    body = {"read_diary": True, "write_diary": permissions["write"]}
                elif path == "/tools":
                    body = [{"name": "events"}]
                elif path == "/actions":
                    body = {"actions": []}
                else:
                    body = {
                        "rows": [
                            {
                                "id": "sample-1",
                                "kind": "user.focus_session",
                                "start": f"{event_day}T10:00:00Z",
                                "end": None,
                                "source": "manual",
                                "status": "confirmed",
                                "payload": {"focus": 3},
                                "topology": "point",
                                "can_update": permissions["update"],
                            }
                        ],
                        "truncated": False,
                    }
                route.fulfill(json=body)

            page.route("**/dashboard", serve_page)
            page.route("**/capabilities", serve_api)
            page.route("**/tools", serve_api)
            page.route("**/tools/events", serve_api)
            page.route("**/actions", serve_api)
            page.goto(url)
            page.locator("#connect").evaluate(
                "(button) => { button.hidden = false; button.disabled = false; }"
            )
            page.locator("#connect").click()
            page.locator("#token").fill("x" * 32)
            page.locator('#auth-form button[type="submit"]').click()
            expect(page.locator("#diary-rows")).to_contain_text("focus: 3")
            expect(page.locator("#diary-rows button")).to_have_count(0)

            permissions["write"] = True
            permissions["update"] = False
            page.locator('#range button[type="submit"]').click()
            expect(page.locator("#diary-rows")).to_contain_text("focus: 3")
            expect(page.locator("#diary-rows button")).to_have_count(0)

            permissions["update"] = True
            page.locator('#range button[type="submit"]').click()
            expect(page.locator("#diary-rows button")).to_have_count(1)
        finally:
            browser.close()
