"""Exercise backup failure on an empty, disposable Linux tmpfs mount."""

import argparse
import errno
import hashlib
import json
import os
import random
import signal
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_backup_commands import capture, check_recovered_files, cli_environment, run_cli, settings

MIB = 1024 * 1024


def free_bytes(directory):
    stats = os.statvfs(directory)
    return stats.f_bavail * stats.f_frsize


def safe_test_volume(path):
    volume = path.resolve(strict=True)
    if not volume.is_mount() or any(volume.iterdir()):
        raise ValueError("Mount an empty scratch tmpfs specifically for this test")
    with Path("/proc/self/mountinfo").open() as mounts:
        tmpfs = any(
            fields[0].split()[4] == str(volume) and fields[1].split()[0] == "tmpfs"
            for fields in (line.split(" - ", 1) for line in mounts)
        )
    if not tmpfs or not 128 * MIB <= free_bytes(volume) <= 384 * MIB:
        raise ValueError("The scratch tmpfs must have between 128 and 384 MiB available")
    return volume


def fill(file, directory):
    length = free_bytes(directory)
    os.posix_fallocate(file.fileno(), 0, length)
    try:
        os.pwrite(file.fileno(), b"x", length)
    except OSError as error:
        assert error.errno == errno.ENOSPC, error
    else:
        raise AssertionError("The fixture volume still accepts writes")


def run_when_full_during_write(binary, root, config, destination):
    with subprocess.Popen(
        [binary, "backup", destination],
        cwd=root,
        env=cli_environment(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and process.poll() is None:
                if any(
                    path.stat().st_size > 0 for path in destination.parent.glob(".rust-backup-*/*")
                ):
                    break
                time.sleep(0.001)
            else:
                raise AssertionError("Encrypted staging did not begin")
            process.send_signal(signal.SIGSTOP)
            stopped, status = os.waitpid(process.pid, os.WUNTRACED)
            assert stopped == process.pid and os.WIFSTOPPED(status)
            with (destination.parent / "synthetic-filler").open("w+b") as file:
                fill(file, destination.parent)
                process.send_signal(signal.SIGCONT)
                stdout, stderr = process.communicate(timeout=15)
                assert process.returncode == 1 and not stdout, (process.returncode, stderr)
                assert "Backup filesystem operation failed" in stderr, stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(binary, root, offhost, engine):
    config = settings(root / "origin", engine).model_copy(update={"backup_dir": offhost})
    assert root.stat().st_dev != offhost.stat().st_dev
    capture(engine, config)
    previous = offhost / "previous.enc"
    space = json.loads(run_cli(binary, root, config, "backup-space", previous))
    assert space["status"] == "ready", space
    counts = json.loads(run_cli(binary, root, config, "backup", previous))
    previous_digest = digest(previous)
    active = config.data_dir / "backup-work/.garmin-ai-plaintext/active"

    filler = offhost / "synthetic-filler"
    refused = offhost / "refused.enc"
    with filler.open("w+b") as file:
        fill(file, offhost)
        space = json.loads(run_cli(binary, root, config, "backup-space", refused))
        assert space["status"] == "insufficient"
        run_cli(binary, root, config, "backup", refused, succeeds=False)
        assert set(offhost.iterdir()) == {previous, filler} and not active.exists()
    filler.unlink()

    payload = config.data_dir / "raw/large-synthetic.bin"
    rng = random.Random(710)
    with payload.open("xb") as stream:
        for _ in range(384):
            stream.write(rng.randbytes(65536))
    failed = offhost / "failed.enc"
    run_when_full_during_write(binary, root, config, failed)
    assert set(offhost.iterdir()) == {previous, filler} and not active.exists()
    assert digest(previous) == previous_digest
    filler.unlink()

    retry = offhost / "retry.enc"
    assert json.loads(run_cli(binary, root, config, "backup", retry)) == counts
    assert set(offhost.iterdir()) == {previous, retry} and not active.exists()
    recovery = root / "retry-recovery"
    run_cli(binary, root, config, "unpack-backup", retry, recovery)
    check_recovered_files(recovery, config)
    payload.unlink()

    recovery = root / "prior-recovery"
    run_cli(binary, root, config, "unpack-backup", previous, recovery)
    check_recovered_files(recovery, config)
    corrupted = offhost / "tampered.enc"
    content = bytearray(previous.read_bytes())
    content[-1] ^= 1
    corrupted.write_bytes(content)
    denied = root / "tampered-recovery"
    run_cli(binary, root, config, "unpack-backup", corrupted, denied, succeeds=False)
    assert not denied.exists() and digest(previous) == previous_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("scratch_tmpfs", type=Path)
    args = parser.parse_args()
    volume = safe_test_volume(args.scratch_tmpfs)
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as tmp, TemporaryDirectory(dir=volume) as offhost:
            verify(args.binary.resolve(strict=True), Path(tmp), Path(offhost), engine)
    finally:
        engine.dispose()
    print("Verified full-volume preflight, ENOSPC during encryption, intact prior backup and retry")


if __name__ == "__main__":
    main()
