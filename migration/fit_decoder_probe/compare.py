"""Run all FIT decoders on the same synthetic inputs in separate, measured processes."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic

MIB = 1024 * 1024


def run_one(arguments):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        start = monotonic()
        with subprocess.Popen(arguments, stdout=output, stderr=errors) as process:
            _, status, usage = os.wait4(process.pid, 0)
            duration = monotonic() - start
            process.returncode = os.waitstatus_to_exitcode(status)
        observations = {
            "elapsed_seconds": round(duration, 6),
            "max_rss_mib": round(usage.ru_maxrss / (MIB if sys.platform == "darwin" else 1024), 3),
        }
        if process.returncode != 0:
            return {**observations, "outcome": "process_failure", "exit_code": process.returncode}
        output.seek(0)
        return {**observations, **json.load(output)}


def match(reference, candidate):
    fields = ["outcome", "headers", "crcs", "definitions", "messages", "developer_fields", "sha256"]
    return [name for name in fields if candidate.get(name) != reference.get(name)]


def compare(binary, fixtures):
    root = Path(__file__).parent
    cases = {}
    for fixture in sorted(fixtures.glob("*.fit")):
        python = run_one([sys.executable, root / "corpus.py", "observe", fixture])
        candidates = {}
        for decoder in ("fitparser", "rustyfit", "rustyfit-components"):
            observed = run_one([binary, decoder, fixture])
            candidates[decoder] = {**observed, "different": match(python, observed)}
        cases[fixture.stem] = {
            "bytes": fixture.stat().st_size,
            "fitdecode": python,
            **candidates,
        }
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("fixtures", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.binary.resolve(strict=True), args.fixtures), indent=2))


if __name__ == "__main__":
    main()
