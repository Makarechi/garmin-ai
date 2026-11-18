"""Exercise the native source probe using an independent executable and failure fixtures."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

INSTANCE = "source:synthetic:probe"
SECRET = "synthetic_fixture_never_print"

MOCK = r"""
import json
import os
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
if MODE in {"timeout", "cancel"}:
    Path("child-pid").write_text(str(os.getpid()), encoding="ascii")
    time.sleep(60)
    sys.exit(0)
print("synthetic_fixture_never_print", file=sys.stderr)
reply = {"version": 1, "request_id": request["request_id"]}
if MODE == "oversized":
    print("x" * (4 * 1024 * 1024 + 2))
    sys.exit(0)
if MODE == "duplicate":
    print('{"version":2,"version":1,"request_id":1,"error":"unavailable"}')
    sys.exit(0)
if MODE == "wrong_id":
    reply["request_id"] += 1
if MODE == "wrong_version":
    reply["version"] = 2
if MODE == "malformed":
    print("synthetic_fixture_never_print {")
    sys.exit(0)
if MODE == "rejected":
    print(json.dumps({**reply, "error": "unavailable"}))
    sys.exit(0)
if request["method"] == "describe":
    if "GA_EXTENSION_PROBE_ENV" in os.environ:
        sys.exit(8)
    reply["result"] = {
        "kind": "source", "instance_id": request["instance_id"], "implementation_version": "fixture",
        "capabilities": {"observations": MODE != "disabled", "cursor": True, "max_page_size": 2},
    }
else:
    at = "2026-01-02T00:00:00+00:00" if MODE == "out_of_window" else "2026-01-01T02:00:00+00:00"
    records = [{
        "source_record_id": "example", "observed_at": at, "effective_at": at, "source_timezone": "UTC",
        "operation": "delete" if MODE == "undeclared_delete" else "upsert",
        "source_reference": "example", "payload": {"value": 1},
    }]
    reply["result"] = {
        "instance_id": request["instance_id"], "page_kind": "partial",
        "fetched_at": "2026-01-02T00:00:00+00:00",
        "next_cursor": "again" if MODE == "repeated_cursor" else None,
        "records": records,
    }
if MODE == "both_fields":
    reply["error"] = None
print(json.dumps(reply))
if MODE == "extra_line":
    print("synthetic_fixture_never_print")
"""


def verify(binary, sample):
    def args(program, instance=INSTANCE):
        return [str(binary), "check-source-extension", str(program), instance]

    with TemporaryDirectory() as directory:
        folder = Path(directory)
        env = dict(os.environ, GA_EXTENSION_PROBE_ENV=SECRET)

        def mock(mode):
            path = folder / mode
            path.write_text(f"#!{sys.executable}\nMODE = {mode!r}\n" + MOCK, encoding="utf-8")
            path.chmod(0o700)
            return path

        def run(program, instance=INSTANCE):
            return subprocess.run(
                args(program, instance), capture_output=True, text=True, timeout=10, env=env
            )

        correct = run(sample)
        assert correct.returncode == 0 and not correct.stderr, correct.stderr
        assert json.loads(correct.stdout) == {"instance_id": INSTANCE, "pages": 2, "records": 3}
        legacy = run(sample, "legacy_source")
        assert (
            legacy.returncode == 0 and json.loads(legacy.stdout)["instance_id"] == "legacy_source"
        )
        clean_environment = run(mock("environment"))
        assert clean_environment.returncode == 0 and not clean_environment.stderr
        for mode, message in [
            ("wrong_id", "invalid response envelope"),
            ("wrong_version", "invalid response envelope"),
            ("both_fields", "invalid response envelope"),
            ("duplicate", "invalid response envelope"),
            ("malformed", "invalid response envelope"),
            ("oversized", "invalid response envelope"),
            ("extra_line", "invalid response envelope"),
            ("rejected", "refused the request"),
            ("disabled", "source capabilities are invalid"),
            ("out_of_window", "outside the source contract"),
            ("repeated_cursor", "outside the source contract"),
            ("undeclared_delete", "outside the source contract"),
            ("timeout", "exceeded its response deadline"),
        ]:
            result = run(mock(mode))
            assert result.returncode == 1 and not result.stdout and message in result.stderr, (
                mode,
                result.returncode,
                result.stderr.replace(SECRET, "<redacted>"),
            )
            assert SECRET not in result.stderr

        pid_path = folder / "child-pid"
        assert_exited(int(pid_path.read_text(encoding="ascii")))
        pid_path.unlink()
        with subprocess.Popen(
            args(mock("cancel")), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        ) as process:
            deadline = time.monotonic() + 2
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pid_path.exists()
            pid = int(pid_path.read_text(encoding="ascii"))
            process.terminate()
            output, detail = process.communicate(timeout=5)
            assert process.returncode == 1 and not output and b"was cancelled" in detail
            assert_exited(pid)
    print("Verified the independent source extension, validation, deadlines and child cleanup")


def assert_exited(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError("A timed out or cancelled extension process is still active")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    verify(args.binary.resolve(strict=True), args.sample.resolve(strict=True))
