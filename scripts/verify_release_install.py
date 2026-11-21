"""Exercise owner onboarding and a fictional tracker through an isolated release API."""

import argparse
import json
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values


def request(url: str, path: str, key: str, body=None, *, method=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(
        Request(url + path, data=data, headers=headers, method=method), timeout=20
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status {response.status} for {path}")
        return json.load(response)


def onboard(url: str, key: str, profile: dict):
    result = request(
        url,
        "/onboarding",
        key,
        {
            **profile,
            "selected_packs": ["general_diary"],
            "source_instance_ids": [],
            "model_categories": [],
        },
        method="PUT",
    )
    preferences = result["preferences"]
    if any(preferences[field] != value for field, value in profile.items()):
        raise RuntimeError("Onboarding did not save the requested owner preferences")
    if preferences["source_instance_ids"] or preferences["model_categories"]:
        raise RuntimeError("Onboarding enabled an unrequested integration")
    return preferences["revision"]


def tracker_entry(url: str, key: str, action_id: str):
    form = request(url, f"/forms/{action_id}", key)
    result = request(
        url,
        f"/forms/{action_id}/submit",
        key,
        {
            "action_id": action_id,
            "schema_hash": form["schema_hash"],
            "submission_id": form["submission_id"],
            "start": "2026-05-15T15:00:00+00:00",
            "timezone": "UTC",
            "values": {"quality": 4},
            "units": {"quality": "score_1-5"},
        },
    )
    return request(url, f"/events/{result['id']}", key)


def inspect_bundle(url: str, installer: list[str]) -> None:
    key = dotenv_values(".env")["GA_API_KEY"]
    with subprocess.Popen(
        [*installer, "inspect"], start_new_session=True, stdin=subprocess.DEVNULL
    ) as process:
        try:
            deadline = monotonic() + 25
            while monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Bundle inspection exited before readiness")
                try:
                    if request(url, "/health/ready", key) == {"status": "ready"}:
                        break
                except (URLError, ConnectionError, TimeoutError):
                    pass
                sleep(0.2)
            else:
                raise RuntimeError("Bundle inspection did not reach readiness")
            if not request(url, "/tools", key):
                raise RuntimeError("Bundle inspection did not expose the read tools")
            try:
                request(url, "/events", key, {})
            except HTTPError as error:
                if error.code != 404:
                    raise
            else:
                raise RuntimeError("Bundle inspection exposed the diary write route")
        finally:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    raise
    print("Read-only bundle inspection passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--phase", choices=("create", "check"), required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    key = dotenv_values(args.env_file)["GA_API_KEY"]
    if not key:
        raise RuntimeError("Release bundle API key is unavailable")
    request(args.url, "/health/ready", key)
    capabilities = request(args.url, "/capabilities", key)
    if capabilities != {"read_diary": True, "write_diary": True, "manage_definitions": True}:
        raise RuntimeError("Release bundle did not expose local owner capabilities")
    profile = {"locale": "ru", "timezone": "Europe/Bratislava", "units": "imperial"}
    if args.phase == "create":
        initial = request(args.url, "/onboarding", key)
        if initial["complete"] or not initial["can_finish_without_sources"]:
            raise RuntimeError("Fresh owner onboarding requires external sources")
        revision = onboard(args.url, key, {"locale": "en", "timezone": "UTC", "units": "metric"})
        draft = {
            "key": "ci_focus",
            "name": "CI focus",
            "locale": "en",
            "topology": "point",
            "fields": [
                {"key": "quality", "label": "Quality", "kind": "scale", "minimum": 1, "maximum": 5}
            ],
            "shortcut": "Log CI focus",
        }
        preview = request(args.url, "/tracker-setups/preview", key, draft)
        request(
            args.url,
            "/tracker-setups",
            key,
            {"draft": draft, "confirmation_token": preview["confirmation_token"]},
        )
    actions = request(args.url, "/actions", key)["actions"]
    matches = [action for action in actions if action["definition_key"] == "user.ci_focus"]
    if len(matches) != 1:
        raise RuntimeError("Fictional tracker action is missing or duplicated")
    action_id = matches[0]["id"]
    if args.phase == "create":
        saved_entry = tracker_entry(args.url, key, action_id)
        if onboard(args.url, key, profile) != revision + 1:
            raise RuntimeError("Updating the owner preferences did not advance their revision")
        args.state_file.write_text(
            json.dumps({"action_id": action_id, "entry": saved_entry}) + "\n"
        )
    else:
        stored = json.loads(args.state_file.read_text())
        if action_id != stored["action_id"]:
            raise RuntimeError("Fictional tracker changed identity after restart")
        saved_entry = stored["entry"]
    status = request(args.url, "/onboarding", key)
    if not status["complete"] or any(status[field] != value for field, value in profile.items()):
        raise RuntimeError("Saved onboarding preferences did not survive startup configuration")
    if request(args.url, f"/events/{saved_entry['id']}", key) != saved_entry:
        raise RuntimeError("Updating onboarding or restarting changed the saved tracker entry")
    print(f"Release API {args.phase} phase passed")


if __name__ == "__main__":
    main()
