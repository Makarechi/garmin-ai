"""Check local sign-in terminal handling without sending credentials anywhere."""

import argparse
import errno
import os
import pty
import select
import signal
import subprocess
import termios
import time
from pathlib import Path
from tempfile import TemporaryDirectory

SYNTHETIC_INPUT = b"synthetic-private-tty-value"


def read_until(terminal, expected, output):
    deadline = time.monotonic() + 5
    while expected not in output:
        readable, _, _ = select.select([terminal], [], [], max(0, deadline - time.monotonic()))
        assert readable, "Missing expected terminal prompt or response"
        output.extend(os.read(terminal, 8192))


def verify_terminal(binary, root, environment):
    pid, terminal = pty.fork()
    if pid == 0:
        os.chdir(root)
        os.execve(binary, [binary, "login"], environment)
    finished = False
    try:
        output = bytearray()
        read_until(terminal, b"Garmin email: ", output)
        assert termios.tcgetattr(terminal)[3] & termios.ECHO
        os.write(terminal, b"synthetic@example.invalid\n")
        read_until(terminal, b"Garmin password: ", output)
        assert not (termios.tcgetattr(terminal)[3] & termios.ECHO)
        os.write(terminal, SYNTHETIC_INPUT + b"\x03")
        read_until(terminal, b"Garmin sign-in cancelled", output)
        assert SYNTHETIC_INPUT not in output
        assert termios.tcgetattr(terminal)[3] & termios.ECHO
        try:
            assert select.select([terminal], [], [], 5)[0], (
                "Sign-in did not exit after cancellation"
            )
            assert not os.read(terminal, 8192)
        except OSError as error:
            assert error.errno == errno.EIO
        _, status = os.waitpid(pid, 0)
        finished = True
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) != 0
    finally:
        os.close(terminal)
        if not finished:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def without_terminal(binary, root, environment, *arguments):
    result = subprocess.run(
        [binary, *arguments],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0 and not result.stdout
    return result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        environment = {
            "GA_DATA_DIR": str(root / "data"),
            "GA_TOKEN_DIR": str(root / "tokens"),
            "GA_LOCK_DIR": str(root / "locks"),
            "GA_BACKUP_DIR": str(root / "backups"),
        }
        assert "requires a local terminal" in without_terminal(binary, root, environment, "login")
        verify_terminal(binary, root, environment)
        raw = root / "data" / "raw"
        raw.mkdir(parents=True)
        sample = raw / "retained-synthetic.json"
        sample.write_text("{}")
        error = without_terminal(binary, root, environment, "login")
        assert "Confirm ownership of retained raw storage locally" in error
        error = without_terminal(binary, root, environment, "login", "--confirm-existing-owner")
        assert "requires a local terminal" in error
        assert "Invalid arguments" in without_terminal(binary, root, environment, "enroll-account")
        assert "requires a database" in without_terminal(
            binary, root, environment, "enroll-account", "--confirm-existing-owner"
        )
        assert not (root / "tokens").exists() and sample.read_text() == "{}"
    print("Terminal echo, cancellation, and retained-data confirmation verified")


if __name__ == "__main__":
    main()
