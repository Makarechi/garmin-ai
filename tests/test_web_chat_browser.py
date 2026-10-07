"""Exercise the authenticated web chat through a real browser and HTTP server."""

import socket
import threading
import time

import uvicorn
from playwright.sync_api import expect, sync_playwright

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings

KEY = "synthetic-browser-owner-" + "c" * 32


def test_browser_connect_note_history_and_disconnect(db, db_engine):
    db.commit()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = create_app(Settings(api_tokens=[ApiToken(key=KEY, scopes={"admin"})]), db_engine)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1180, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.goto(f"http://127.0.0.1:{port}/chat")
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
            held = []

            def hold_refresh(route):
                if route.request.method == "GET":
                    held.append(route)
                else:
                    route.continue_()

            page.route("**/web-chat/messages", hold_refresh)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            for _ in range(40):
                if held:
                    break
                page.wait_for_timeout(50)
            assert held
            page.locator("#disconnect").click()
            held[0].fulfill(
                json={
                    "messages": [
                        {"id": "late", "text": "late private message", "created_at": "2026-10-07T18:00:00Z"}
                    ],
                    "replies": [],
                }
            )
            page.wait_for_timeout(200)
            expect(page.locator("#chat")).to_be_hidden()
            assert page.locator("#messages .bubble").count() == 0
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
