"""Create synthetic wire fields and capture fitdecode's raw base-type results."""

import io
import json
import random
import struct
import sys

import fitdecode
from fitdecode.types import BASE_TYPE_BYTE, BASE_TYPES
from fitdecode.utils import compute_crc


def field_type(identifier, data):
    base = BASE_TYPES.get(identifier, BASE_TYPE_BYTE)
    return base if len(data) % base.size == 0 else BASE_TYPE_BYTE


def raw_shape(value, base):
    if value is None:
        return ["invalid"]
    if isinstance(value, str):
        return ["text", value]
    if base is BASE_TYPE_BYTE:
        return ["bytes", list(value)]
    if isinstance(value, tuple):
        return ["array", [raw_shape(item, base) for item in value]]
    if isinstance(value, float):
        return ["float", int.from_bytes(struct.pack(">d", value), "big")]
    if base.name.startswith("sint"):
        return ["signed", value]
    return ["unsigned", value]


def observe(identifier, endian, data):
    order = ">" if endian else "<"
    definition = struct.pack(f"{order}BBBHB", 0x40, 0, endian, 0xFF01, 1)
    definition += bytes([222, len(data), identifier])
    body = definition + b"\x00" + data
    header = struct.pack("<BBHI4s", 12, 0x20, 2100, len(body), b".FIT")
    packet = header + body + struct.pack("<H", compute_crc(header + body))
    with fitdecode.FitReader(
        io.BytesIO(packet),
        check_crc=fitdecode.CrcCheck.RAISE,
        error_handling=fitdecode.ErrorHandling.IGNORE,
    ) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage):
                field = frame.fields[0]
                return [field.type.name, raw_shape(field.raw_value, field_type(identifier, data))]
    raise RuntimeError("Synthetic FIT field was not decoded")


def emit(output):
    random_source = random.Random(211710)
    for identifier in (*BASE_TYPES, 0x03, 0xFE):
        base = BASE_TYPES.get(identifier, BASE_TYPE_BYTE)
        lengths = (1, base.size, 2 * base.size, 3 * base.size, 31, 255)
        for endian in (0, 1):
            cases = [
                data
                for length in lengths
                for data in (
                    bytes(length),
                    bytes([127]) * length,
                    bytes([255]) * length,
                    random_source.randbytes(length),
                )
            ]
            cases.extend([b"\xff\x00x", b"\xf0\x9f\x92", b"\xe2\x80A", b"\xc0\xc1"])
            if base.name.startswith("float"):
                order = ">" if endian else "<"
                cases.extend(
                    struct.pack(f"{order}{base.fmt}", x)
                    for x in (0.0, -0.0, float("inf"), -float("inf"))
                )
            for data in cases:
                expected = observe(identifier, endian, data)
                print(json.dumps([identifier, endian, list(data), expected]), file=output)


if __name__ == "__main__":
    emit(sys.stdout)
