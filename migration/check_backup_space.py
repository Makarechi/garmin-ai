"""Compare backup-space using only synthetic source files and a disposable database."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine

from garmin_ai.backup_space import backup_space
from garmin_ai.config import Settings


def run(binary):
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as temp, TemporaryDirectory(dir="/dev/shm") as split:
            root = Path(temp)
            data = root / "data"
            tokens = root / "tokens"
            raw = data / "raw" / "ab"
            raw.mkdir(parents=True)
            tokens.mkdir()
            (data / "coverage-report.json").write_bytes(b"synthetic")
            (raw / "fixture.json").write_bytes(b"simulation" * 291)
            (tokens / "fixture.txt").write_bytes(b"synthetic marker")
            settings = Settings(
                data_dir=data, token_dir=tokens, database_url=os.environ["GA_TEST_DATABASE_URL"]
            )

            def execute(destination):
                return subprocess.run(
                    [binary, "backup-space", destination],
                    cwd=root,
                    env={
                        "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                        "GA_DATA_DIR": str(data),
                        "GA_TOKEN_DIR": str(tokens),
                    },
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

            for path in [root / "new-folder" / "backup.ga", Path(split) / "nested" / "backup.ga"]:
                reference = backup_space(engine, settings, path)
                result = execute(path)
                assert result.returncode == 0, result.stderr
                actual = json.loads(result.stdout)
                for volume in actual["volumes"] + reference["volumes"]:
                    assert volume.pop("free_bytes") >= 0
                assert actual == reference, (actual, reference)
                assert not path.parent.exists()
            (raw / "redirect").symlink_to(root)
            result = execute(root / "backup.ga")
            assert result.returncode == 1
            assert result.stdout == ""
            assert str(root) not in result.stderr
            (raw / "redirect").unlink()
            redirect = root / "redirect"
            redirect.symlink_to(Path(split))
            result = execute(redirect / "backup.ga")
            assert result.returncode == 1 and result.stdout == ""
            assert not (Path(split) / "backup.ga").exists()
            print("Matched shared and separate backup volumes and rejected two symlink redirects")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
