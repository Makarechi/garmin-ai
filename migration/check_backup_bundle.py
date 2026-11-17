"""Record a synthetic native backup inventory and read it with pinned Python."""

import argparse
import base64
import gzip
import hashlib
import random
from pathlib import Path

from garmin_ai.config import Settings
from garmin_ai.operations import unpack_backup

FIXTURE_BYTES = bytes(range(32))
LONG_MEMBER = "synthetic_" + "é" * 70 + ".txt"


def record(root):
    root.mkdir(exist_ok=False)
    data = root / "data"
    raw = data / "raw"
    raw.mkdir(parents=True)
    secrets = root / "secrets"
    secrets.mkdir()
    payload = b'{"synthetic": true}\n'
    digest = hashlib.sha256(payload).hexdigest()
    key = f"{digest[:2]}/{digest}.json"
    required = raw / key
    required.parent.mkdir()
    required.write_bytes(payload)
    (root / "required.txt").write_text(key, encoding="ascii")
    (data / "coverage-report.json").write_bytes(payload)
    (secrets / "test-token").write_bytes(b"synthetic token descriptor")
    (secrets / LONG_MEMBER).write_bytes("Синтетический файл".encode())
    with gzip.open(root / "database.jsonl.gz", "wb") as stream:
        stream.write(b'{"format": "garmin-ai-jsonl-v1", "synthetic": true}\n')
    medium = random.Random(640)
    with (raw / "large-test.bin").open("wb") as stream:
        for _ in range(13 * 64):
            stream.write(medium.randbytes(65536))


def verify(root):
    recovered = root / "python-recovered"
    settings = Settings(
        _env_file=None,
        data_dir=root / "data",
        token_dir=root / "secrets",
        backup_key=base64.urlsafe_b64encode(FIXTURE_BYTES).decode(),
    )
    unpack_backup(settings, root / "rust.backup", recovered)
    expected = {name: root / name for name in ("database.jsonl.gz",)}
    expected["coverage-report.json"] = root / "data/coverage-report.json"
    for prefix, source in (("raw", root / "data/raw"), ("tokens", root / "secrets")):
        expected.update(
            {
                f"{prefix}/{path.relative_to(source)}": path
                for path in source.rglob("*")
                if path.is_file()
            }
        )
    observed = {str(path.relative_to(recovered)) for path in recovered.rglob("*") if path.is_file()}
    assert expected.keys() == observed
    total = 0
    for name, source in expected.items():
        target = recovered / name
        with source.open("rb") as original, target.open("rb") as restored:
            while data := original.read(65536):
                assert data == restored.read(len(data))
                total += len(data)
            assert not restored.read(1)
        assert target.stat().st_mode & 0o777 == 0o600
    print(f"Pinned Python recovered {len(expected)} native bundle members and {total} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "verify"))
    parser.add_argument("root", type=Path)
    options = parser.parse_args()
    (record if options.mode == "record" else verify)(options.root)
