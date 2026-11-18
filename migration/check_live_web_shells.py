"""Compare the live public asset routes and open both Rust pages in Chromium."""

import argparse
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import create_engine

from garmin_ai.api import create_app
from garmin_ai.config import Settings

ASSETS = (
    "/dashboard",
    "/dashboard-assets/app.js",
    "/dashboard-assets/styles.css",
    "/chat",
    "/chat-assets/app.js",
    "/chat-assets/styles.css",
)
HEADERS = (
    "content-type",
    "cache-control",
    "referrer-policy",
    "x-content-type-options",
    "content-security-policy",
)
KEY = "synthetic-web-shell-fixture-owner-token"


def compare(base):
    engine = create_engine("postgresql+psycopg://127.0.0.1:1")
    try:
        with (
            TestClient(create_app(Settings(api_key=KEY), engine)) as python,
            httpx.Client(base_url=base, timeout=5) as rust,
        ):
            for path in ASSETS:
                original, candidate = python.get(path), rust.get(path)
                assert original.status_code == candidate.status_code == 200, path
                assert original.content == candidate.content, path
                for header in HEADERS:
                    assert original.headers[header] == candidate.headers[header], (path, header)
                for method in ("POST", "HEAD"):
                    original = python.request(method, path)
                    candidate = rust.request(method, path)
                    assert original.status_code == candidate.status_code == 405
                    assert original.headers["allow"] == candidate.headers["allow"] == "GET"
            for path in ("/dashboard-assets/unknown", "/chat-assets/unknown"):
                assert python.get(path).status_code == rust.get(path).status_code == 404
            for path in ("/dashboard", "/chat"):
                original = python.get(path + "/?view=wide", follow_redirects=False)
                candidate = rust.get(path + "/?view=wide", follow_redirects=False)
                assert original.status_code == candidate.status_code == 307
                assert urlsplit(original.headers["location"]).path == path
                assert candidate.headers["location"] == path + "?view=wide"
            assert rust.get("/capabilities").status_code == 401
            assert rust.get("/capabilities", headers={"Authorization": f"Bearer {KEY}"}).json() == (
                python.get("/capabilities", headers={"Authorization": f"Bearer {KEY}"}).json()
            )
    finally:
        engine.dispose()


def browser(base):
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            page = chromium.new_page(viewport={"width": 1180, "height": 900})
            requests, errors = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            dashboard = page.goto(base + "/dashboard")
            assert "'self'" in dashboard.headers["content-security-policy"]
            expect(page.locator("#connect")).to_be_visible()
            expect(page.locator("#mode")).to_have_text("Демонстрационные данные")
            chat = page.goto(base + "/chat")
            assert "connect-src 'self'" in chat.headers["content-security-policy"]
            expect(page.locator("#connect-form")).to_be_visible()
            expect(page.locator("#chat")).to_be_hidden()
            assert not errors, errors
            assert {url.removeprefix(base) for url in requests} == set(ASSETS)
        finally:
            chromium.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    with TemporaryDirectory() as directory:
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("GA_")
        }
        environment["GA_API_KEY"] = KEY
        with subprocess.Popen(
            [binary, "serve", "--port", "0"],
            cwd=directory,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
        ) as process:
            try:
                line = process.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:"), line
                base = line.removeprefix("Listening on ")
                compare(base)
                browser(base)
                print("Live dashboard and chat assets matched; both opened locally in Chromium")
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
