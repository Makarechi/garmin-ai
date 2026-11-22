"""Compare the native coverage command against the frozen Python report and archive."""

import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import socket
import ssl
import subprocess
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qsl, urlsplit

from check_tracker_extraction_http import certificates

from garmin_ai.accounts import profile_fingerprint
from garmin_ai.archive import LocalArchive
from garmin_ai.probe import probe

HOST = "connectapi.garmin.com"
DAY = date(2026, 11, 1)
OWNER = {"profileId": 12345, "displayName": "Synthetic Profile"}
DAILY = {
    "synthetic_integer": 42,
    "synthetic_float": 1.25,
    "synthetic_scalar": "do not disclose this invented value",
    "variants": [True, False, None, {}, [], {}, {"inner": [{"leaf": "value"}]}],
}
ACTIVITIES = [{"activityId": 42, "synthetic_scalar": "do not disclose this invented value"}]
SCOPED_ACTIVITIES = [
    {"activityId": 41, "startTimeGMT": "2026-10-31 22:59:59"},
    {"activityId": 42, "startTimeGMT": "2026-10-31 23:00:00"},
    {"activityId": 43, "startTimeGMT": "2026-11-01 23:00:00"},
]
BINARY = b"PK\x03\x04synthetic source-free fixture"


class ReferenceReader:
    def account_fingerprint(self):
        return profile_fingerprint(OWNER)

    def fetch(self, endpoint, day=None, activity_id=None):
        if endpoint.name in {"sleep", "devices"}:
            return []
        if endpoint.name == "hrv":
            return None
        return DAILY

    def call(self, method, *args, **_kwargs):
        if method == "get_activities":
            assert args == (0, 100)
            return ACTIVITIES
        assert method == "download_activity" and args == ("42",)
        return BINARY


def routes():
    filename = Path(__file__).parent / "contracts" / "garmin-legacy-requests.json"
    contract = json.loads(filename.read_text())
    contract["activities"][1]["start"] = "0"
    path, query = contract["activities"]
    contract["scoped_activities"] = (
        path,
        query | {"startDate": DAY.isoformat(), "endDate": DAY.isoformat()},
    )
    return {
        (url, tuple(sorted(parameters.items()))): name
        for name, (url, parameters) in contract.items()
    }


@contextmanager
def fake_garmin(directory):
    assert Path("/.dockerenv").is_file(), "Run in a disposable container on an internal network"
    ca, certificate, key = certificates(directory, HOST)
    request_routes = routes()
    seen = []
    state = {"mode": "normal"}

    class Garmin(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.headers["Authorization"].startswith("Bearer ")
            address = urlsplit(self.path)
            if address.path == "/userprofile-service/socialProfile":
                seen.append("profile")
                identity = (
                    OWNER if state["mode"] != "different-owner" else OWNER | {"profileId": 3467}
                )
                body, content_type = json.dumps(identity).encode(), "application/json"
            else:
                key = (address.path, tuple(sorted(parse_qsl(address.query))))
                endpoint = request_routes[key]
                seen.append(endpoint)
                if state["mode"] == "lost-database":
                    state["lose_database"]()
                if state["mode"] == "rate-limited":
                    self.send_response(429)
                    self.send_header("Retry-After", "900")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if endpoint == "activity_fit":
                    body, content_type = BINARY, "application/zip"
                else:
                    payload = {
                        "steps": DAILY,
                        "sleep": [],
                        "hrv": None,
                        "devices": [],
                        "activities": ACTIVITIES,
                        "scoped_activities": SCOPED_ACTIVITIES,
                    }.get(endpoint, DAILY)
                    body, content_type = json.dumps(payload).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 443), Garmin)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    hosts = Path("/etc/hosts")
    original = hosts.read_text()
    hosts.write_text(original + f"\n127.0.0.1 {HOST}\n")
    thread = Thread(target=server.serve_forever, daemon=True)
    try:
        assert all(
            ipaddress.ip_address(route[4][0]).is_loopback for route in socket.getaddrinfo(HOST, 443)
        )
        thread.start()
        yield ca, seen, state
    finally:
        hosts.write_text(original)
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()


def stores(root, ca):
    token_dir = root / "tokens"
    token_dir.mkdir(mode=0o700)
    claims = {
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "client_id": "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    session = token_dir / "garmin_tokens.json"
    session.write_text(
        json.dumps(
            {
                "di_token": f"e30.{encoded}.synthetic",
                "di_refresh_token": "invented-local-session",
                "di_client_id": claims["client_id"],
            }
        )
    )
    session.chmod(0o600)
    return {
        "GA_DATA_DIR": str(root / "data"),
        "GA_TOKEN_DIR": str(token_dir),
        "GA_LOCK_DIR": str(root / "state"),
        "GA_BACKUP_DIR": str(root / "backups"),
        "GA_TIMEZONE": "Europe/Bratislava",
        "SSL_CERT_FILE": str(ca),
    }, session


def run(binary, root, environment, end=DAY):
    return subprocess.run(
        [binary, "probe", "--start", DAY.isoformat(), "--end", end.isoformat()],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=75,
    )


def read_report(path):
    assert path.stat().st_mode & 0o777 == 0o600
    report = json.loads(path.read_text())
    for record in report["requests"]:
        assert datetime.fromisoformat(record.pop("fetched_at")).utcoffset() == timedelta(0)
    return report


def exercise_scoped(binary, root, ca, seen):
    work = root / "scoped"
    work.mkdir()
    environment, _ = stores(work, ca)
    result = subprocess.run(
        [
            binary,
            "probe",
            "--start",
            DAY.isoformat(),
            "--end",
            DAY.isoformat(),
            "--activities-in-range",
        ],
        cwd=work,
        env=environment,
        text=True,
        capture_output=True,
        timeout=75,
    )
    assert result.returncode == 0, result.stderr
    assert "devices" not in seen and "activities" not in seen
    assert seen.count("scoped_activities") == 1 and seen.count("activity_fit") == 1
    report = read_report(work / "data" / "coverage-report.json")
    assert report["complete"]
    archive = work / "data" / "raw"
    assert json.loads((archive / report["activity_list_archive"]).read_bytes()) == [
        SCOPED_ACTIVITIES[1]
    ]
    (fit,) = (record for record in report["requests"] if record["endpoint"] == "activity_fit")
    assert fit["key"] == "42" and (archive / fit["archive_key"]).read_bytes() == BINARY


def exercise(binary, root, ca, seen, state, database=None):
    work = root / "normal"
    work.mkdir()
    environment, tokens = stores(work, ca)
    if database:
        environment["GA_DATABASE_URL"] = database
    path = work / "data" / "coverage-report.json"
    original_token = tokens.read_bytes()

    invalid = run(binary, work, environment, DAY + timedelta(days=31))
    assert invalid.returncode == 2 and "Probe range must be 1–31 days" in invalid.stderr
    assert not seen and not path.exists()
    for date_spelling in ("20261101", "2026-W44-7", "2026W447", "2026-W44", "2026W44"):
        reversed_range = subprocess.run(
            [binary, "probe", "--start", date_spelling, "--end", "2026-10-01"],
            cwd=work,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert reversed_range.returncode == 2
        assert "Probe range must be 1–31 days" in reversed_range.stderr and not seen
    completed = run(binary, work, environment)
    assert completed.returncode == 0, completed.stderr
    assert "Review before sharing." in completed.stdout and not completed.stderr
    assert (
        "do not disclose" not in completed.stdout
        and "invented-local-session" not in completed.stdout
    )
    assert tokens.read_bytes() == original_token
    expected_archive = LocalArchive(root / "reference")
    expected = probe(ReferenceReader(), expected_archive, DAY, DAY)
    for record in expected["requests"]:
        record.pop("fetched_at")
    assert read_report(path) == expected
    assert seen == ["profile", *[record["endpoint"] for record in expected["requests"]]]
    for record in expected["requests"]:
        archive_key = record["archive_key"]
        archived = work / "data" / "raw" / archive_key
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == archived.stem
        assert archived.read_bytes() == expected_archive.read(archive_key)
    previous = path.read_bytes()

    with (work / "state" / "storage.lock").open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seen.clear()
        occupied = run(binary, work, environment)
        assert occupied.returncode == 1 and not seen
        assert path.read_bytes() == previous
    state["mode"] = "different-owner"
    denied = run(binary, work, environment)
    assert denied.returncode == 1 and seen == ["profile"]
    assert path.read_bytes() == previous and tokens.read_bytes() == original_token

    if database:
        from sqlalchemy import create_engine, text

        def lose_database():
            engine = create_engine(database)
            try:
                with engine.connect() as connection:
                    terminated = (
                        connection.execute(
                            text(
                                "SELECT pg_terminate_backend(locks.pid) FROM pg_locks locks "
                                "JOIN pg_stat_activity session ON session.pid = locks.pid "
                                "WHERE locks.locktype='advisory' AND locks.objid=72104620 "
                                "AND locks.granted AND session.datname=current_database()"
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert terminated == [True]
            finally:
                engine.dispose()

        state.update(mode="lost-database", lose_database=lose_database)
        seen.clear()
        before_raw = sorted((work / "data" / "raw").glob("*/*"))
        stopped = run(binary, work, environment)
        assert stopped.returncode == 1 and seen == ["profile", "daily"]
        assert read_report(path)["requests"] == [] and not read_report(path)["complete"]
        assert sorted((work / "data" / "raw").glob("*/*")) == before_raw
        return len(expected["requests"])
    state["mode"] = "rate-limited"
    seen.clear()
    limited = run(binary, work, environment)
    assert limited.returncode == 1 and seen == ["profile", "daily"]
    interrupted = read_report(path)
    assert not interrupted["complete"]
    assert [(row["endpoint"], row["error_type"]) for row in interrupted["requests"]] == [
        ("daily", "GarminConnectTooManyRequestsError"),
        ("steps", "CircuitOpen"),
    ]
    assert (work / "data" / "raw" / expected["activity_list_archive"]).is_file()
    return len(expected["requests"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--database", action="store_true")
    args = parser.parse_args()
    database = None
    if args.database:
        from capture_reference import isolated_engine

        engine = isolated_engine()
        try:
            database = engine.url.render_as_string(hide_password=False)
        finally:
            engine.dispose()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with fake_garmin(root) as (ca, seen, state):
            exercise_scoped(args.binary.resolve(strict=True), root, ca, seen)
            seen.clear()
            count = exercise(args.binary.resolve(strict=True), root, ca, seen, state, database)
    print(f"Matched all {count} reference archive requests and verified owner and storage fencing")


if __name__ == "__main__":
    main()
