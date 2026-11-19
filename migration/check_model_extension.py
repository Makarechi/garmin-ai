"""Exercise native model probing against an independent example and synthetic failures."""

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

INSTANCE = "model:synthetic:probe"
SENTINEL = "synthetic_model_probe_no_logs"

MOCK = """
import json
import os
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
assert "GA_MODEL_PROBE_SENTINEL" not in os.environ
reply = {"version": 1, "request_id": request["request_id"]}
print("synthetic_model_probe_no_logs", file=sys.stderr)
if request["method"] == "describe":
    reply["result"] = {
        "kind": "channel" if MODE == "wrong_kind" else "model",
        "instance_id": request["instance_id"],
        "implementation_version": "synthetic-1",
        "capabilities": {"structured_output": MODE != "no_schema"},
    }
else:
    assert request["method"] == "structured"
    params = request["params"]
    assert params["prompt"] == "Synthetic probe"
    assert "contains no owner data" in params["instruction"]
    assert params["schema"] == {
        "additionalProperties": False, "properties": {"urgent": {"title": "Urgent", "type": "boolean"}},
        "required": ["urgent"], "title": "ModelProbe", "type": "object",
    }
    Path("model-called").touch()
    if MODE in {"timeout", "cancel"}:
        Path("child-pid").write_text(str(os.getpid()), encoding="ascii")
        time.sleep(60)
        sys.exit(0)
    if MODE == "oversized":
        print("x" * (1024 * 1024 + 2))
        sys.exit(0)
    result = {"value": {"urgent": True}, "usage": {"input_tokens": 9, "output_tokens": 0}}
    if MODE == "false":
        result = {"value": {"urgent": False}}
    if MODE == "empty":
        result["value"] = {}
    if MODE == "bad_type":
        result["value"]["urgent"] = []
    if MODE == "extra":
        result["value"]["extra"] = "synthetic"
    if MODE == "negative":
        result["usage"]["input_tokens"] = -1
    if MODE == "large":
        result["usage"]["output_tokens"] = 1000000001
    if MODE == "non_integer":
        result["usage"]["input_tokens"] = 1.0
    if MODE == "unknown_usage":
        result["usage"]["unexpected"] = 1
    if MODE == "wrong_version":
        reply["version"] += 1
    if MODE == "refusal":
        reply["error"] = "unavailable"
    else:
        reply["result"] = result
print(json.dumps(reply))
"""


def verify(binary, sample):
    env = dict(os.environ, GA_MODEL_PROBE_SENTINEL=SENTINEL)
    with TemporaryDirectory() as folder:
        root = Path(folder)
        called = root / "model-called"

        def mock(mode):
            path = root / mode
            path.write_text(f"#!{sys.executable}\nMODE = {mode!r}\n" + MOCK, encoding="utf-8")
            path.chmod(0o700)
            return path

        def command(program, instance=INSTANCE):
            return [binary, "check-model-extension", program, instance]

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

        for program in (sample, mock("true"), mock("false")):
            result = run(program)
            assert result.returncode == 0 and not result.stderr
            assert json.loads(result.stdout) == {"structured_output": True}
        called.unlink()
        assert run(mock("true"), "source:synthetic:probe").returncode == 1
        assert not called.exists()

        invalid = "outside the model contract"
        for mode, expected, attempted in (
            ("wrong_kind", "model capabilities are invalid", False),
            ("no_schema", "model capabilities are invalid", False),
            ("empty", invalid, True),
            ("bad_type", invalid, True),
            ("extra", invalid, True),
            ("negative", invalid, True),
            ("large", invalid, True),
            ("non_integer", invalid, True),
            ("unknown_usage", invalid, True),
            ("wrong_version", "invalid response envelope", True),
            ("refusal", "refused the request", True),
            ("oversized", "invalid response envelope", True),
            ("timeout", "exceeded its response deadline", True),
        ):
            result = run(mock(mode))
            assert result.returncode == 1 and not result.stdout and expected in result.stderr, mode
            assert SENTINEL not in result.stderr
            assert called.exists() == attempted
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
        "Verified the independent model example, both Boolean results, usage validation, size bounds, timeout and cancellation"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    verify(args.binary.resolve(strict=True), args.sample.resolve(strict=True))
