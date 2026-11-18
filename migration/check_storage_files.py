"""Prove that Python and Rust exclude the same maintenance operation."""

import argparse
import os
import select
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from garmin_ai.config import Settings
from garmin_ai.storage_files import exclusive_files


def wait_until_locked(process):
    response = b""
    deadline = time.monotonic() + 5
    while b"NATIVE_LOCK_ACQUIRED" not in response:
        remaining = max(0, deadline - time.monotonic())
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        assert ready, "Native process did not announce the lock"
        added = os.read(process.stdout.fileno(), 4096)
        assert added, "Native process exited before acquiring the lock"
        response += added
        assert len(response) <= 65536


def run(binary):
    with TemporaryDirectory() as root:
        directory = Path(root).resolve() / "locks"
        settings = Settings(_env_file=None, lock_dir=directory)
        environment = {**os.environ, "GA_TEST_STORAGE_FILES": str(directory)}
        command = [binary, "--ignored", "--exact", "shares_one_lock_with_python", "--nocapture"]
        with exclusive_files(settings):
            subprocess.run(
                command,
                env={**environment, "GA_TEST_STORAGE_MODE": "blocked"},
                check=True,
                timeout=5,
            )
        with subprocess.Popen(
            command,
            env={**environment, "GA_TEST_STORAGE_MODE": "hold"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as process:
            try:
                wait_until_locked(process)
                try:
                    with exclusive_files(settings):
                        raise AssertionError("Python acquired the lock held by Rust")
                except ValueError as error:
                    assert str(error) == "Stop the worker, login or probe before this operation"
                process.communicate(input=b"\n", timeout=5)
                assert process.returncode == 0
            finally:
                if process.poll() is None:
                    process.kill()
        with exclusive_files(settings):
            pass
    print("Python and Rust excluded one another and released the original storage lock")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
