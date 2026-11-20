"""Probe authenticated local controls and synthetic webhook ingress during measured work."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

BUSY = "Telegram ingestion busy; retry delivery"
UNAVAILABLE = "Database unavailable or identity is not ready"


class ControlProbeError(RuntimeError):
    pass


def service_unavailable_reason(error):
    try:
        detail = json.loads(error.read(400)).get("detail")
    except (ValueError, AttributeError):
        detail = None
    known = {BUSY, UNAVAILABLE, "Telegram integration is disabled", "Telegram channel is disabled"}
    return detail if detail in known else f"HTTP {error.code}"


def probe(base_url, path, *, payload=None, headers=None, retry_on=()):
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    started = time.monotonic()
    retries = 0
    while True:
        remaining = 2 - (time.monotonic() - started)
        if retry_on and remaining <= 0:
            raise ControlProbeError("Control retry deadline expired")
        try:
            with urllib.request.urlopen(request, timeout=remaining if retry_on else 6) as response:
                body = json.load(response)
            return body, round((time.monotonic() - started) * 1000, 3), retries
        except urllib.error.HTTPError as error:
            if not retry_on or error.code != 503:
                raise
            reason = service_unavailable_reason(error)
            if reason not in retry_on:
                raise ControlProbeError(reason) from error
            retries += 1
            time.sleep(0.075)


def controls(base_url, *, paired_fit=False):
    owner = {"Authorization": "Bearer " + os.environ["GA_API_KEY"]}
    pause_retries = 0
    webhook_retries = 0

    def pause():
        nonlocal pause_retries
        response, elapsed, pause_retries = probe(
            base_url,
            "/web-chat/messages",
            payload={"client_message_id": str(uuid4()), "text": "/pause"},
            headers=owner,
            retry_on=(UNAVAILABLE,) if paired_fit else (),
        )
        if (response["duplicate"] and not paired_fit) or not response["reply"]:
            raise RuntimeError("Authenticated pause did not return its stored reply")
        return elapsed

    def operations():
        status, elapsed, _retries = probe(base_url, "/operations", headers=owner)
        if not status:
            raise RuntimeError("Operations status returned no data")
        return elapsed

    def webhook():
        nonlocal webhook_retries
        owner_id = int(os.environ["GA_TELEGRAM_USER_ID"])
        headers = {"X-Telegram-Bot-Api-Secret-Token": os.environ["GA_TELEGRAM_WEBHOOK_SECRET"]}
        reply, elapsed, webhook_retries = probe(
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
            retry_on=(BUSY,) if paired_fit else (),
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
    }
    if not paired_fit:
        probes["cli_inventory_ms"] = cli
    with ThreadPoolExecutor(max_workers=len(probes)) as executor:
        futures = {name: executor.submit(action) for name, action in probes.items()}
        if not paired_fit:
            return {name: future.result() for name, future in futures.items()}
        elapsed, failures = {}, {}
        for name, future in futures.items():
            try:
                elapsed[name] = future.result()
            except ControlProbeError as error:
                failures[name] = str(error)
            except urllib.error.HTTPError as error:
                failures[name] = f"HTTP {error.code}"
            except (OSError, RuntimeError) as error:
                failures[name] = type(error).__name__
        return {
            "latencies_ms": elapsed,
            "failures": failures,
            "web_pause_retries": pause_retries,
            "webhook_retries": webhook_retries,
        }


if __name__ == "__main__":
    print(json.dumps(controls(sys.argv[1], paired_fit=sys.argv[2:] == ["--paired-fit"])))
