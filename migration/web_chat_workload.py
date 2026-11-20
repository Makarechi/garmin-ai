"""Write the same bounded synthetic web conversation in each runtime."""

import argparse
import hashlib
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

ROUTE = "/web-chat/messages"
HISTORY_LIMIT = 100
NOTE_LENGTH = 4000
MAX_MESSAGE_LENGTH = 16000
LONG_MESSAGE_INTERVAL = 8


def call(base, path, *, token=None, body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode()
    request = Request(f"{base}{path}", headers=headers, data=data)
    start = monotonic()
    with urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError("Synthetic web request did not return HTTP 200")
        decoded = json.load(response)
    return decoded, round((monotonic() - start) * 1000, 3)


def percentile(samples):
    if not samples:
        raise RuntimeError("No web workload samples were collected")
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]


def controls(base, token, finished):
    latency = {"liveness_ms": [], "operations_ms": []}
    while not finished.is_set():
        for name, path, auth in (
            ("liveness_ms", "/health/live", None),
            ("operations_ms", "/operations", token),
        ):
            _body, duration = call(base, path, token=auth)
            latency[name].append(duration)
        finished.wait(0.1)
    return {
        name: {"samples": len(values), "p95": percentile(values)}
        for name, values in latency.items()
    }


def submit(base, token, index, text):
    identity = str(uuid5(NAMESPACE_URL, f"garmin-ai/resource-web-chat/{index}"))
    body = {"client_message_id": identity, "text": text}
    document, duration = call(base, ROUTE, token=token, body=body)
    if document["duplicate"] or document["reply"]["inbound_message_id"] != document["message_id"]:
        raise RuntimeError("Synthetic submission did not return its own stored response")
    return body, document, duration


def run(base, count):
    token = os.environ["GA_API_KEY"]
    finished = Event()
    observations = []
    digest = hashlib.sha256()
    notes = 0
    started = monotonic()
    with ThreadPoolExecutor(max_workers=1) as observer:
        pending = observer.submit(controls, base, token, finished)
        try:
            for index in range(count):
                if (index + 1) % LONG_MESSAGE_INTERVAL == 0:
                    text = "x" * MAX_MESSAGE_LENGTH
                else:
                    label = f"Synthetic benchmark note {index:04d} "
                    note = label + "x" * (NOTE_LENGTH - len(label))
                    text = f"/note {note}"
                    digest.update(note.encode())
                    notes += 1
                request, response, duration = submit(base, token, index, text)
                observations.append(duration)
                if (index + 1) % LONG_MESSAGE_INTERVAL == LONG_MESSAGE_INTERVAL - 1:
                    last_request, last_response = request, response
            retry, _elapsed = call(base, ROUTE, token=token, body=last_request)
            if retry != {**last_response, "duplicate": True}:
                raise RuntimeError("Synthetic idempotent retry changed its stored response")
            history, history_ms = call(base, ROUTE, token=token)
            visible = min(count, HISTORY_LIMIT)
            if len(history["messages"]) != visible or len(history["replies"]) != visible:
                raise RuntimeError("The retained web history did not honor the existing page cap")
            if history["messages"][-1]["text"] != "x" * MAX_MESSAGE_LENGTH:
                raise RuntimeError("The maximum-length message was not retained")
        finally:
            finished.set()
        overlap = pending.result()
    return {
        "notes": notes,
        "message_count": count,
        "maximum_length_messages": count - notes,
        "note_characters": NOTE_LENGTH,
        "maximum_message_characters": MAX_MESSAGE_LENGTH,
        "note_digest": digest.hexdigest(),
        "visible_messages": visible,
        "duration_seconds": round(monotonic() - started, 3),
        "write_latency_p95_ms": percentile(observations),
        "history_ms": history_ms,
        "overlap": overlap,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("messages", type=int, choices=(80, 800))
    args = parser.parse_args()
    print(json.dumps(run(args.base, args.messages)))
