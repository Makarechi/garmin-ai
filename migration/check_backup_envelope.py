"""Generate and verify synthetic backups using the pinned Python cipher."""

import argparse
import json
from pathlib import Path

from garmin_ai.operations import decrypt_file, encrypt_file, verify_encrypted_file

FIXTURE_BYTES = bytes(range(32))


def record(root):
    root.mkdir(exist_ok=True)
    source = root / "source.bin"
    block = bytes(range(256)) * 256
    with source.open("wb") as stream:
        for _ in range(1057):
            stream.write(block)
        stream.write(b"synthetic final block")
    encrypted = root / "python.backup"
    encrypt_file(source, encrypted, FIXTURE_BYTES)
    expected = verify_encrypted_file(encrypted, FIXTURE_BYTES)
    (root / "expected.json").write_text(json.dumps(expected))
    (root / "empty.bin").write_bytes(b"")
    encrypt_file(root / "empty.bin", root / "empty.backup", FIXTURE_BYTES)
    print(f"Generated {expected['plaintext_bytes']} synthetic bytes")


def verify(root):
    expected = json.loads((root / "expected.json").read_text())
    actual = verify_encrypted_file(root / "rust.backup", FIXTURE_BYTES)
    assert actual == expected
    decrypt_file(root / "rust.backup", root / "python-decoded.bin", FIXTURE_BYTES)
    with (
        (root / "source.bin").open("rb") as source,
        (root / "python-decoded.bin").open("rb") as decoded,
    ):
        while block := source.read(65536):
            assert block == decoded.read(len(block))
        assert not decoded.read(1)
    print(
        f"Pinned Python authenticated and decrypted {actual['plaintext_bytes']} Rust-encrypted bytes"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "verify"))
    parser.add_argument("root", type=Path)
    options = parser.parse_args()
    if options.mode == "record":
        record(options.root)
    else:
        verify(options.root)


if __name__ == "__main__":
    main()
