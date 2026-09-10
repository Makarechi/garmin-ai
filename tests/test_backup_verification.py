import hashlib
import os

import pytest
from cryptography.exceptions import InvalidTag

from garmin_ai import operations


def test_encryption_verifies_stored_bytes_without_plaintext_output(tmp_path, monkeypatch):
    source, destination = tmp_path / "synthetic", tmp_path / "snapshot.enc"
    content = b"synthetic private text" * (operations.CHUNK // 7)
    source.write_bytes(content)
    key = os.urandom(32)
    original = operations.verify_encrypted_file
    verified = []

    def check(path, secret):
        assert not destination.exists()
        result = original(path, secret)
        verified.append(result)
        return result

    monkeypatch.setattr(operations, "verify_encrypted_file", check)
    operations.encrypt_file(source, destination, key)
    assert verified == [
        {"plaintext_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    ]
    assert set(tmp_path.iterdir()) == {source, destination}
    assert original(destination, key) == verified[0]


@pytest.mark.parametrize("kind", ["ciphertext", "tag", "truncated", "wrong_valid_ciphertext"])
def test_invalid_staged_ciphertext_is_never_published(tmp_path, monkeypatch, kind):
    source, destination = tmp_path / "synthetic", tmp_path / "snapshot.enc"
    source.write_bytes(b"synthetic content")
    key = os.urandom(32)
    original = operations.verify_encrypted_file

    def damage_then_check(path, secret):
        data = bytearray(path.read_bytes())
        if kind == "truncated":
            data = data[: len(operations.MAGIC)]
        elif kind == "wrong_valid_ciphertext":
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            nonce = os.urandom(12)
            cipher = Cipher(algorithms.AES(secret), modes.GCM(nonce)).encryptor()
            cipher.authenticate_additional_data(operations.MAGIC)
            encrypted = cipher.update(b"other synthetic content") + cipher.finalize()
            data = operations.MAGIC + nonce + encrypted + cipher.tag
        else:
            data[len(operations.MAGIC) + 12 if kind == "ciphertext" else -1] ^= 1
        path.write_bytes(data)
        return original(path, secret)

    monkeypatch.setattr(operations, "verify_encrypted_file", damage_then_check)
    with pytest.raises((ValueError, InvalidTag)):
        operations.encrypt_file(source, destination, key)
    assert not destination.exists()
    assert set(tmp_path.iterdir()) == {source}


def test_verification_failure_preserves_concurrently_created_destination(tmp_path, monkeypatch):
    source, destination = tmp_path / "synthetic", tmp_path / "snapshot.enc"
    source.write_bytes(b"synthetic content")

    def fail(path, key):
        destination.write_bytes(b"concurrent owner file")
        raise ValueError("synthetic verification failure")

    monkeypatch.setattr(operations, "verify_encrypted_file", fail)
    with pytest.raises(ValueError):
        operations.encrypt_file(source, destination, os.urandom(32))
    assert destination.read_bytes() == b"concurrent owner file"
    assert set(tmp_path.iterdir()) == {source, destination}
