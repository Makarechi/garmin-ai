"""Drive the real native trend delivery worker against a local synthetic Telegram service."""

import argparse
import ipaddress
import json
import socket
import ssl
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from zoneinfo import ZoneInfo

from capture_reference import isolated_engine
from check_native_worker_health import wait_ready
from check_tracker_extraction_http import certificates
from check_trend_insight_worker import SLOT, ZONE, execute, reference, seed, state
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.models import ChannelBinding, Person

HOST = "api.telegram.org"
TOKEN = "829031:synthetic-local-trend-fixture"
OWNER = 813004


@contextmanager
def fake_telegram(directory):
    ca, cert, key = certificates(directory, HOST)
    observed = {"poll": Event(), "attempts": [], "mode": "limited"}

    class Telegram(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path.startswith(f"/bot{TOKEN}/")
            endpoint = self.path.rsplit("/", 1)[-1]
            if endpoint == "getWebhookInfo":
                status, response = (
                    200,
                    {"ok": True, "result": {"url": "", "pending_update_count": 0}},
                )
            elif endpoint == "getUpdates":
                observed["poll"].set()
                time.sleep(0.03)
                status, response = 200, {"ok": True, "result": []}
            else:
                assert endpoint == "sendMessage" and request["chat_id"] == OWNER
                observed["attempts"].append(request)
                match observed["mode"]:
                    case "limited":
                        status = 429
                        response = {
                            "ok": False,
                            "error_code": 429,
                            "parameters": {"retry_after": 30},
                        }
                    case "unknown":
                        status, response = 503, {"ok": False, "error_code": 503}
                    case _:
                        status, response = (
                            200,
                            {
                                "ok": True,
                                "result": {
                                    "message_id": 400,
                                    "chat": {"id": OWNER, "type": "private"},
                                },
                            },
                        )
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 443), Telegram)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    host_file = Path("/etc/hosts")
    alias = f"127.0.0.1 {HOST} # synthetic-garmin-trend-delivery\n"
    host_file.write_text(host_file.read_text() + "\n" + alias)
    thread = Thread(target=server.serve_forever, daemon=True)
    try:
        assert all(
            ipaddress.ip_address(result[4][0]).is_loopback
            for result in socket.getaddrinfo(HOST, 443)
        )
        thread.start()
        yield ca, observed
    finally:
        host_file.write_text(host_file.read_text().replace(alias, ""))
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=2)
        server.server_close()


@contextmanager
def telegram_worker(binary, root, engine, ca, observed):
    observed["poll"].clear()
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(root),
        "GA_TIMEZONE": "UTC",
        "GA_TELEGRAM_BOT_TOKEN": TOKEN,
        "GA_TELEGRAM_USER_ID": str(OWNER),
        "GA_PROACTIVE_ENABLED": "true",
        "GA_QUESTION_BUDGET": "2",
        "GA_QUIET_START_HOUR": "0",
        "GA_QUIET_END_HOUR": "0",
        "GA_INTEGRATIONS": json.dumps(
            [{"id": "channel:telegram:primary", "kind": "channel", "provider": "telegram"}]
        ),
        "SSL_CERT_FILE": str(ca),
    }
    with subprocess.Popen(
        [binary, "worker"], cwd=root, env=environment, stdout=subprocess.PIPE, text=True
    ) as process:
        try:
            wait_ready(process)
            assert observed["poll"].wait(timeout=5)
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                assert process.wait(timeout=5) == 0


def insight(engine, metric):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT id,status,statement,evidence FROM insights WHERE split_part(dedup_key,':',2)=:metric"
            ),
            {"metric": metric},
        ).one()


def marker(engine, identity):
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT value FROM app_state WHERE key=:key"),
            {"key": f"outbox:insight:{identity}:0"},
        )


def wait_marker(engine, process, identity, expected):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert process.poll() is None
        value = marker(engine, identity)
        if value is not None and value["status"] == expected:
            return value
        time.sleep(0.05)
    raise AssertionError(f"Expected {expected} delivery, found {marker(engine, identity)}")


def prepare(engine, now):
    seed(engine, now)
    state(
        engine,
        "preferences:onboarding",
        {
            "channel": {"channel": "telegram", "instance_id": "primary"},
            "model_categories": [],
            "timezone": ZONE,
        },
    )
    with Session(engine) as session, session.begin():
        session.add(
            ChannelBinding(
                owner_id=session.query(Person).one().id,
                channel="telegram",
                channel_instance_id="primary",
                external_id=str(OWNER),
                confirmation_method="fixture",
            )
        )
    state(engine, SLOT, {"slot": int(now.timestamp()) // 21600})
    reference(engine, now)


def exercise(binary, root, engine, ca, observed):
    now = datetime.now(UTC)
    prepare(engine, now)
    original = insight(engine, "sleep_score")
    assert original.status == "accepted"
    state(engine, "proactive:enabled", {"enabled": False})
    with telegram_worker(binary, root, engine, ca, observed):
        time.sleep(0.4)
        assert marker(engine, original.id) is None and not observed["attempts"]
    state(engine, "proactive:enabled", {"enabled": True})
    execute(engine, "UPDATE health_days SET sleep_score=sleep_score-1")
    with telegram_worker(binary, root, engine, ca, observed):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and insight(engine, "sleep_score").status == "accepted":
            time.sleep(0.05)
        assert insight(engine, "sleep_score").status == "cancelled"
        assert marker(engine, original.id) is None and not observed["attempts"]
    execute(engine, "UPDATE health_days SET sleep_score=sleep_score+1")
    reference(engine, now)
    assert insight(engine, "sleep_score").status == "accepted"
    with telegram_worker(binary, root, engine, ca, observed) as process:
        pending = wait_marker(engine, process, original.id, "pending")
    assert datetime.fromisoformat(pending["retry_at"]) > now
    assert insight(engine, "sleep_score").status == "accepted"
    assert [call["text"] for call in observed["attempts"]] == [original.statement]

    observed["mode"] = "success"
    with telegram_worker(binary, root, engine, ca, observed):
        time.sleep(0.4)
        assert len(observed["attempts"]) == 1
    state(
        engine,
        f"outbox:insight:{original.id}:0",
        {**pending, "retry_at": (now - timedelta(minutes=1)).isoformat()},
    )
    with telegram_worker(binary, root, engine, ca, observed) as process:
        sent = wait_marker(engine, process, original.id, "sent")
    assert sent["message_id"] == 400 and insight(engine, "sleep_score").status == "delivered"
    assert [call["text"] for call in observed["attempts"]] == [original.statement] * 2

    today = now.astimezone(ZoneInfo(ZONE)).date()
    execute(
        engine,
        "UPDATE health_days SET resting_hr=CASE WHEN day>=:cutoff THEN 62 ELSE 58 END",
        cutoff=today - timedelta(days=14),
    )
    reference(engine, now)
    next_insight = insight(engine, "resting_hr")
    assert next_insight.status == "accepted"
    observed["mode"] = "unknown"
    with telegram_worker(binary, root, engine, ca, observed) as process:
        wait_marker(engine, process, next_insight.id, "uncertain")
    assert insight(engine, "resting_hr").status == "uncertain"
    observed["mode"] = "success"
    with telegram_worker(binary, root, engine, ca, observed):
        time.sleep(0.4)
    assert len(observed["attempts"]) == 3
    execute(
        engine,
        "UPDATE health_days SET sleep_seconds=CASE WHEN day>=:cutoff THEN 25200 ELSE 21600 END",
        cutoff=today - timedelta(days=14),
    )
    reference(engine, now)
    interrupted = insight(engine, "sleep_seconds")
    assert interrupted.status == "accepted"
    state(
        engine,
        f"outbox:insight:{interrupted.id}:0",
        {"status": "sending", "started_at": now.isoformat(), "formatted": True},
    )
    state(
        engine,
        "insight:last:sleep_seconds",
        {"at": now.isoformat(), "reservation": str(interrupted.id)},
    )
    with telegram_worker(binary, root, engine, ca, observed):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and insight(engine, "sleep_seconds").status == "accepted":
            time.sleep(0.05)
        assert insight(engine, "sleep_seconds").status == "uncertain"
    assert len(observed["attempts"]) == 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir(mode=0o700)
            with fake_telegram(root) as (ca, observed):
                exercise(args.binary.resolve(strict=True), root, engine, ca, observed)
    finally:
        engine.dispose()
    print("Verified paused delivery, rate-limited retry and durable sent/uncertain trend receipts")


if __name__ == "__main__":
    main()
