"""Verify first-owner Telegram pairing using a loopback bot and disposable PostgreSQL."""

import argparse
import ipaddress
import json
import re
import select
import socket
import ssl
import subprocess
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

from capture_reference import isolated_engine
from check_tracker_extraction_http import certificates
from dotenv import dotenv_values
from sqlalchemy import text

HOST = "api.telegram.org"
TOKEN = "808712:synthetic-local-pairing"
OWNER = 901277


def message(code, *, sender=OWNER, chat=OWNER, kind="private", **extra):
    return {
        "message": {
            "text": code,
            "from": {"id": sender, "is_bot": False},
            "chat": {"type": kind, "id": chat},
            **extra,
        }
    }


@contextmanager
def bot(root):
    ca, cert, key = certificates(root, HOST)
    details = {"webhook": True, "command": None, "ready": Event(), "calls": []}

    class Telegram(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path.startswith(f"/bot{TOKEN}/")
            endpoint = self.path.rsplit("/", 1)[-1]
            details["calls"].append((endpoint, request))
            if endpoint == "getWebhookInfo":
                result = {
                    "url": "https://synthetic.invalid/hook" if details["webhook"] else "",
                    "pending_update_count": 0,
                }
            elif endpoint == "getMe":
                result = {
                    "id": 171,
                    "is_bot": True,
                    "first_name": "Fixture",
                    "username": "fixture_bot",
                }
            elif endpoint == "getUpdates" and request["timeout"] == 0:
                assert request == {"timeout": 0, "offset": 17, "allowed_updates": ["message"]}
                result = []
            elif endpoint == "getUpdates":
                assert request["allowed_updates"] == ["message"] and "offset" not in request
                assert details["ready"].wait(timeout=4)
                code = details["command"]
                result = [
                    {"update_id": 10, **message(code, kind="group")},
                    {"update_id": 11, **message(code, chat=99920)},
                    {"update_id": 12, **message(code, forward_origin={"type": "hidden_user"})},
                    {"update_id": 15, **message("/pair incorrect")},
                    {"update_id": 16, **message(code)},
                    {"update_id": 20, **message("later synthetic message")},
                ]
            else:
                raise AssertionError(endpoint)
            payload = json.dumps({"ok": True, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 443), Telegram)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    hosts = Path("/etc/hosts")
    alias = f"127.0.0.1 {HOST} # synthetic-garmin-cli-pairing\n"
    hosts.write_text(hosts.read_text() + "\n" + alias)
    thread = Thread(target=server.serve_forever, daemon=True)
    try:
        assert all(
            ipaddress.ip_address(result[4][0]).is_loopback
            for result in socket.getaddrinfo(HOST, 443)
        )
        thread.start()
        yield ca, details
    finally:
        hosts.write_text(hosts.read_text().replace(alias, ""))
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()


def bindings(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT b.id,b.external_id,b.confirmation_method FROM channel_bindings b"
                " JOIN people p ON p.id=b.owner_id WHERE p.singleton AND b.channel='telegram'"
                " AND b.channel_instance_id='primary' AND b.confirmed_at IS NOT NULL"
            )
        ).all()


def verify(binary, root, engine):
    file = root / ".env"
    original = (
        f"# Synthetic local pairing\nGA_TELEGRAM_BOT_TOKEN='{TOKEN}'\nGA_TELEGRAM_USER_ID=0\n"
    )
    file.write_text(original)
    args = (binary, "pair-telegram", "--env-file", file, "--container-runtime")
    with bot(root) as (ca, details):
        directory_settings = {
            "GA_DATA_DIR": str(root / "data"),
            "GA_TOKEN_DIR": str(root / "tokens"),
            "GA_LOCK_DIR": str(root / "state"),
            "GA_BACKUP_DIR": str(root / "backup"),
        }
        runtime = {
            **directory_settings,
            "SSL_CERT_FILE": str(ca),
            "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        }

        def run():
            return subprocess.run(args, env=runtime, capture_output=True, text=True, timeout=12)

        primary = {"id": "channel:telegram:primary", "kind": "channel", "provider": "telegram"}
        alternate = {**primary, "id": "channel:telegram:alternate"}
        selections = (
            "[]",
            "",
            json.dumps([{**primary, "enabled": False}]),
            json.dumps([alternate, primary]),
        )
        disabled = (f"GA_INTEGRATIONS='{selection}'\n" for selection in selections)
        for setting in disabled:
            blocked = original + setting
            file.write_text(blocked)
            result = run()
            assert result.returncode == 1 and "GA_INTEGRATIONS" in result.stderr
            assert file.read_text() == blocked and not details["calls"] and not bindings(engine)

        # Different casing still follows assignment order, including a replacement database.
        overridden = (
            original
            + "GA_DATABASE_URL='postgresql://garmin@127.0.0.1:1/unreachable_test'\n"
            + f"ga_database_url='{runtime['GA_DATABASE_URL']}'\n"
        )
        file.write_text(overridden)
        with engine.begin() as lock:
            lock.execute(text("SELECT pg_advisory_xact_lock(72104620)"))
            occupied = subprocess.run(
                args[:-1], env=runtime, capture_output=True, text=True, timeout=12
            )
        assert occupied.returncode == 1 and "already running" in occupied.stderr
        assert file.read_text() == overridden and not details["calls"] and not bindings(engine)
        file.write_text(original)

        with engine.begin() as lock:
            lock.execute(text("SELECT pg_advisory_xact_lock(72104620)"))
            occupied = run()
        assert occupied.returncode == 1 and "already running" in occupied.stderr
        assert not details["calls"] and not bindings(engine)
        webhook = run()
        assert webhook.returncode == 1 and "without a webhook" in webhook.stderr
        assert [endpoint for endpoint, _ in details["calls"]] == ["getWebhookInfo"]
        assert file.read_text() == original and not bindings(engine)
        details["webhook"] = False

        def pair(*, modify_env=False):
            details["calls"].clear()
            details["ready"].clear()
            with subprocess.Popen(
                args, env=runtime, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as process:
                try:
                    assert select.select([process.stdout], [], [], 5)[0]
                    line = process.stdout.readline()
                    command = re.search(r"(/pair [A-Za-z0-9_-]{32})\s*$", line)
                    assert command and "@fixture_bot" in line and TOKEN not in line
                    details["command"] = command[1]
                    if modify_env:
                        file.write_text(original + "# Concurrent local edit\n")
                    details["ready"].set()
                    return (process.communicate(timeout=8), process.returncode)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=2)

        output, code = pair(modify_env=True)
        assert code == 1 and "changed during pairing" in output[1]
        assert file.read_text().endswith("# Concurrent local edit\n") and not bindings(engine)

        file.write_text(original)
        output, code = pair()
        assert code == 0 and "Telegram owner paired" in output[0] and not output[1]
        assert [endpoint for endpoint, _ in details["calls"]] == [
            "getWebhookInfo",
            "getMe",
            "getUpdates",
            "getUpdates",
        ]
        assert file.stat().st_mode & 0o777 == 0o600
        assert dotenv_values(file)["GA_TELEGRAM_USER_ID"] == str(OWNER)
        paired = bindings(engine)
        assert len(paired) == 1 and paired[0][1:] == (str(OWNER), "local_pairing_code")

        details["calls"].clear()
        assert run().returncode == 0 and bindings(engine) == paired
        published = file.read_text()
        file.write_text(published.replace(f"'{OWNER}'", "'100273'"))
        rejected = run()
        assert rejected.returncode == 1 and "does not match" in rejected.stderr
        assert bindings(engine) == paired and not details["calls"]
        file.write_text(published)
        with engine.begin() as fixture:
            fixture.execute(text("DELETE FROM channel_bindings WHERE id=:id"), {"id": paired[0].id})
        assert run().returncode == 0 and not details["calls"]
        assert [tuple(row[1:]) for row in bindings(engine)] == [(str(OWNER), "local_pairing_code")]
    print(
        "Verified runtime settings, private pairing, exact acknowledgement, concurrent edit rollback and idempotent recovery"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(args.binary.resolve(strict=True), Path(directory), engine)
    finally:
        engine.dispose()
