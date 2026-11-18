"""Build Python-format tar packages and check the native recovery trees."""

import argparse
import hashlib
import tarfile
from pathlib import Path

from check_backup_bundle import FIXTURE_BYTES, record

from garmin_ai.operations import encrypt_file


def members(root):
    expected = {
        "database.jsonl.gz": root / "database.jsonl.gz",
        "coverage-report.json": root / "data/coverage-report.json",
    }
    for prefix, source in (("raw", root / "data/raw"), ("tokens", root / "secrets")):
        expected.update(
            {
                f"{prefix}/{path.relative_to(source)}": path
                for path in source.rglob("*")
                if path.is_file()
            }
        )
    return expected


def make_backup(root):
    record(root)
    for variant, mode in (("compressed", "w|gz"), ("uncompressed", "w|")):
        archive_path = root / f"{variant}.tar"
        with tarfile.open(archive_path, mode=mode) as archive:
            for name, source in members(root).items():
                archive.add(source, arcname=name, recursive=False)
        encrypt_file(archive_path, root / f"{variant}.backup", FIXTURE_BYTES)
        archive_path.unlink()


def verify(root):
    expected = members(root)
    for variant in ("compressed", "uncompressed"):
        directory = root / f"rust-{variant}"
        recovered = {
            str(path.relative_to(directory)): path
            for path in directory.rglob("*")
            if path.is_file()
        }
        assert recovered.keys() == expected.keys()
        total = 0
        for name, origin in expected.items():
            target = recovered[name]
            with origin.open("rb") as first, target.open("rb") as second:
                assert (
                    hashlib.file_digest(first, "sha256").digest()
                    == hashlib.file_digest(second, "sha256").digest()
                )
            assert target.stat().st_mode & 0o777 == 0o600
            total += target.stat().st_size
        print(f"Verified {len(expected)} {variant} recovered files and {total} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "verify"))
    parser.add_argument("root", type=Path)
    options = parser.parse_args()
    (make_backup if options.mode == "record" else verify)(options.root)
