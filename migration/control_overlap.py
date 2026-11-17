"""Probe authenticated local controls and synthetic webhook ingress during measured work."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


def probe(base_url, path, *, payload=None, headers=None):
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=6) as response:
        body = json.load(response)
        return body, round((time.monotonic() - started) * 1000, 3)


def controls(base_url):
    owner = {"Authorization": "Bearer " + os.environ["GA_API_KEY"]}

    def pause():
        response, elapsed = probe(
            base_url,
            "/web-chat/messages",
            payload={"client_message_id": str(uuid4()), "text": "/pause"},
            headers=owner,
        )
        if response["duplicate"] or not response["reply"]:
            raise RuntimeError("Authenticated pause did not return its stored reply")
        return elapsed

    def operations():
        status, elapsed = probe(base_url, "/operations", headers=owner)
        if not status:
            raise RuntimeError("Operations status returned no data")
        return elapsed

    def webhook():
        owner_id = int(os.environ["GA_TELEGRAM_USER_ID"])
        headers = {"X-Telegram-Bot-Api-Secret-Token": os.environ["GA_TELEGRAM_WEBHOOK_SECRET"]}
        reply, elapsed = probe(
            base_url,
            "/telegram/webhook",
            headers=headers,
            payload={
                "update_id": 12345,
                "message": {
                    "message_id": 7,
                    "from": {"id": owner_id},
                    "chat": {"id": owner_id, "type": "private"},
                    "date": int(time.time()),
                    "text": "/status",
                },
            },
        )
        if reply != {"ok": True, "accepted": True}:
            raise RuntimeError("Authenticated webhook did not persist the synthetic ingress")
        return elapsed

    def cli():
        start = time.monotonic()
        inventory = subprocess.run(
            ["garmin-ai", "inventory"], check=True, capture_output=True, text=True, timeout=5
        )
        if len(inventory.stdout.splitlines()) != 29:
            raise RuntimeError("CLI inventory returned incomplete results")
        return round((time.monotonic() - start) * 1000, 3)

    probes = {
        "authenticated_web_pause_ms": pause,
        "operations_response_ms": operations,
        "webhook_ingress_ms": webhook,
        "cli_inventory_ms": cli,
    }
    with ThreadPoolExecutor(max_workers=len(probes)) as executor:
        futures = {name: executor.submit(action) for name, action in probes.items()}
        return {name: future.result() for name, future in futures.items()}


if __name__ == "__main__":
    print(json.dumps(controls(sys.argv[1])))
