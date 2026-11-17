"""Prepare synthetic FIT cases and compare compatible ActivityPart payloads."""

import argparse
import json
import struct
from pathlib import Path

import fitdecode
from fit_workload import definition
from fitdecode.utils import compute_crc

from garmin_ai.fit import message_values

ACCEPTED_PROBE_CASES = (
    "50k.fit",
    "chained.fit",
    "compressed-timestamp.fit",
    "developer.fit",
    "header-12.fit",
    "header-zero.fit",
    "invalid-value.fit",
    "unknown-big-endian.fit",
    "unknown-record-field.fit",
)


def packet(data):
    header = struct.pack("<BBHI4s", 12, 0x20, 2100, len(data), b".FIT")
    return header + data + struct.pack("<H", compute_crc(header + data))


def extra_cases():
    clocks = definition(0, 3, [(28, 4, 0x86), (29, 4, 0x86), (253, 4, 0x86)])
    clocks += struct.pack("<BIII", 0, 3601, 86400, 1_170_000_023)
    file_id = definition(1, 0, [(1, 2, 0x84), (2, 2, 0x84), (4, 4, 0x86)])
    file_id += struct.pack("<BHHI", 1, 1, 1036, 9356789)
    hr = definition(2, 132, [(253, 4, 0x86), (9, 4, 0x86), (10, 3, 0x0D)])
    hr += struct.pack("<BII", 2, 1_160_000_000, 1234) + b"\x12\x34\x56"
    record = definition(3, 20, [(99, 3, 0x0D), (3, 1, 2), (253, 4, 0x86)])
    record += struct.pack("<B3sBI", 3, b"\x00\x01\x02", 0xFF, 1_160_000_001)
    record += definition(3, 20, [(99, 4, 0x84)]) + struct.pack("<BHH", 3, 0xFF11, 0x1201)
    missing_description = definition(4, 20, [(3, 1, 2)], developer=(19, 2, 7))
    missing_description += b"\x24\x45\x00\xff"
    return {
        "clocks-and-subfield.fit": packet(clocks + file_id),
        "hr-and-respiration.fit": packet(hr + record),
        "missing-description.fit": packet(missing_description),
    }


def reference(source, destination):
    index = 0
    with (
        fitdecode.FitReader(
            source, check_crc=fitdecode.CrcCheck.RAISE, error_handling=fitdecode.ErrorHandling.IGNORE
        ) as reader,
        destination.open("w", encoding="utf-8") as output,
    ):
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage):
                name = frame.name or f"unknown_{frame.global_mesg_num}"
                print(json.dumps([name, message_values(frame, index)], allow_nan=False), file=output)
                index += 1
    return index


def prepare(probe_dir, output):
    output.mkdir(parents=True, exist_ok=True)
    cases = {name: (probe_dir / name).read_bytes() for name in ACCEPTED_PROBE_CASES}
    cases.update(extra_cases())
    counts = []
    for name, data in sorted(cases.items()):
        path = output / name
        path.write_bytes(data)
        counts.append([name, reference(path, path.with_suffix(".ndjson"))])
    (output / "manifest.json").write_text(json.dumps(counts) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("probe", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.probe, args.output)
