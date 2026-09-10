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


@pytest.mark.parametrize("replacement", [False, True])
def test_mutation_after_verification_aborts_publication(tmp_path, monkeypatch, replacement):
    source, destination = tmp_path / "synthetic", tmp_path / "snapshot.enc"
    source.write_bytes(b"synthetic content")
    original = operations.verify_encrypted_file

    def change_after_check(path, key):
        result = original(path, key)
        data = bytearray(path.read_bytes())
        data[len(operations.MAGIC) + 12] ^= 1
        if replacement:
            other = path.with_suffix(".replacement")
            other.write_bytes(data)
            other.replace(path)
        else:
            path.write_bytes(data)
        return result

    monkeypatch.setattr(operations, "verify_encrypted_file", change_after_check)
    with pytest.raises(ValueError, match="changed before publication"):
        operations.encrypt_file(source, destination, os.urandom(32))
    assert not destination.exists()
    assert set(tmp_path.iterdir()) == {source}


def test_same_size_overwrite_of_already_read_block_is_detected(tmp_path, monkeypatch):
    source, encrypted = tmp_path / "synthetic", tmp_path / "snapshot.enc"
    source.write_bytes(b"synthetic content" * 20)
    key = os.urandom(32)
    operations.encrypt_file(source, encrypted, key)
    original_open = type(encrypted).open

    class Reader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            at = self.stream.tell()
            value = self.stream.read(size)
            if at == len(operations.MAGIC) + 12 and not self.changed:
                self.changed = True
                with original_open(encrypted, "r+b") as writer:
                    writer.seek(at)
                    writer.write(bytes([value[0] ^ 1]))
            return value

    def open_file(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return Reader(stream) if path == encrypted and mode == "rb" else stream

    monkeypatch.setattr(type(encrypted), "open", open_file)
    with pytest.raises(ValueError, match="changed during verification"):
        operations.verify_encrypted_file(encrypted, key)
