"""Exercise the native channel probe through subprocesses and the independent example."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from check_source_extension import assert_exited

INSTANCE = "channel:synthetic:probe"
SENTINEL = "synthetic_channel_probe_no_logs"

MOCK = """
import json
import os
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
assert "GA_CHANNEL_PROBE_SENTINEL" not in os.environ
reply = {"version": 1, "request_id": request["request_id"]}
method = request["method"]
params = request["params"]
print("synthetic_channel_probe_no_logs", file=sys.stderr)
if method == "describe":
    reply["result"] = {
        "kind": "source" if MODE == "wrong_kind" else "channel",
        "instance_id": request["instance_id"],
        "implementation_version": "synthetic-1",
        "capabilities": {
            "text": MODE != "no_text",
            "voice": MODE == "voice",
            "actions": MODE == "extra_action",
            "max_text_length": 2 if MODE == "short_limit" else 4096,
        },
    }
elif method == "delivery_policy":
    reply["result"] = {"allow_delivery": MODE != "denied", "allow_initiative": False}
    if MODE == "bad_policy_clock":
        reply["result"]["retry_after"] = "invalid"
else:
    assert method == "deliver"
    Path("delivery-called").touch()
    if MODE in {"timeout", "cancel"}:
        Path("child-pid").write_text(str(os.getpid()), encoding="ascii")
        time.sleep(60)
        sys.exit(0)
    intent = params["intent"]
    identity = intent["intent_id"]
    assert intent["channel_instance"] == {"channel": "synthetic", "instance_id": "probe"}
    assert intent["initiative"] is False
    state = "delivered" if MODE == "weak_receipt" else "provider_accepted"
    reply["result"] = {
        "intent_id": identity,
        "state": state,
        "receipt": {
            "intent_id": "00000000-0000-0000-0000-000000000001" if MODE == "foreign_receipt"
                         else identity,
            "state": "provider_accepted",
            "observed_at": "invalid" if MODE == "bad_receipt_clock" else params["now"],
        },
        "rendered": {
            "intent_id": identity,
            "texts": ["Different synthetic text"] if MODE == "missing_text"
                     else [part["text"] for part in intent["blocks"]],
            "medium": "voice" if MODE == "voice" else "text",
            "mode": "edit" if MODE == "edit" else "send",
            "actions": [{}] if MODE == "extra_action" else [],
        },
    }
    if MODE == "unknown_field":
        reply["result"]["unexpected"] = "synthetic"
    if MODE == "no_receipt":
        del reply["result"]["receipt"]
    if MODE == "wrong_delivery_id":
        reply["request_id"] += 1
print(json.dumps(reply))
"""


def verify(binary, sample):
    def command(program, instance=INSTANCE):
        return [binary, "check-channel-extension", program, instance]

    env = dict(os.environ, GA_CHANNEL_PROBE_SENTINEL=SENTINEL)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        called = root / "delivery-called"

        def mock(mode):
            path = root / mode
            path.write_text(f"#!{sys.executable}\nMODE = {mode!r}\n" + MOCK, encoding="utf-8")
            path.chmod(0o700)
            return path

        def run(program, instance=INSTANCE):
            return subprocess.run(
                command(program, instance),
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        expected = {
            "instance_id": INSTANCE,
            "state": "provider_accepted",
            "verified_capabilities": ["text"],
            "unverified_capabilities": ["initiatives"],
        }
        example = run(sample)
        assert example.returncode == 0 and not example.stderr, example.stderr
        assert json.loads(example.stdout) == expected
        clean = run(mock("clean"))
        assert clean.returncode == 0 and not clean.stderr
        assert json.loads(clean.stdout) == {**expected, "unverified_capabilities": []}
        called.unlink()

        blocked = run(mock("clean"), "source:synthetic:probe")
        assert blocked.returncode == 1 and not blocked.stdout and not called.exists()
        for mode, message, attempted in (
            ("wrong_kind", "channel capabilities are invalid", False),
            ("no_text", "channel capabilities are invalid", False),
            ("denied", "outside the channel contract", False),
            ("bad_policy_clock", "outside the channel contract", False),
            ("wrong_delivery_id", "invalid response envelope", True),
            ("short_limit", "outside the channel contract", True),
            ("voice", "outside the channel contract", True),
            ("edit", "outside the channel contract", True),
            ("extra_action", "outside the channel contract", True),
            ("missing_text", "outside the channel contract", True),
            ("foreign_receipt", "outside the channel contract", True),
            ("bad_receipt_clock", "outside the channel contract", True),
            ("weak_receipt", "outside the channel contract", True),
            ("no_receipt", "outside the channel contract", True),
            ("unknown_field", "outside the channel contract", True),
            ("timeout", "exceeded its response deadline", True),
        ):
            result = run(mock(mode))
            assert result.returncode == 1 and not result.stdout and message in result.stderr, mode
            assert SENTINEL not in result.stderr
            assert called.exists() == attempted, mode
            called.unlink(missing_ok=True)

        pid = root / "child-pid"
        assert_exited(int(pid.read_text(encoding="ascii")))
        pid.unlink()
        with subprocess.Popen(
            command(mock("cancel")),
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                deadline = time.monotonic() + 2
                while not pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert pid.exists() and process.poll() is None
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=4)
                assert not stdout and "cancelled" in stderr and SENTINEL not in stderr
                assert process.returncode == 1
                assert_exited(int(pid.read_text(encoding="ascii")))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)
    print(
        "Verified the independent channel example, ineligible delivery, malformed responses, bounded timeout and cancellation"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    verify(args.binary.resolve(strict=True), args.sample.resolve(strict=True))
