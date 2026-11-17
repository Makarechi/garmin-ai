"""Generate reproducible FIT fixtures and capture the pinned Python decoder's frame behavior."""

import argparse
import hashlib
import io
import json
import struct
from pathlib import Path
from zipfile import ZipFile

import fitdecode
from fit_workload import definition, fit_member
from fitdecode.utils import compute_crc
from fitlimit_workload import zip_member

from garmin_ai.fit import json_value


def fit_packet(body, *, version=0x20, header_size=14, zero=False, only_data=False):
    header = struct.pack("<BBHI4s", header_size, version, 2100, len(body), b".FIT")
    if header_size > 12:
        header += bytes(header_size - 14)
        header += struct.pack("<H", 0 if zero else compute_crc(header))
    final_crc = compute_crc(body if only_data else header + body)
    return header + body + struct.pack("<H", final_crc)


def create(output, *, ceiling=False):
    output.mkdir(parents=True, exist_ok=True)
    developer = fit_member(0, 3, with_developer=True)
    (output / "developer.fit").write_bytes(developer)
    (output / "chained.fit").write_bytes(developer + fit_member(3, 4, with_developer=True))
    (output / "crc-bad.fit").write_bytes(developer[:-1] + bytes([developer[-1] ^ 1]))
    (output / "truncated.fit").write_bytes(developer[:-1])
    (output / "missing-crc.fit").write_bytes(developer[:-2])
    (output / "header-crc-bad.fit").write_bytes(
        developer[:12] + bytes([developer[12] ^ 1]) + developer[13:]
    )
    base = fit_member(0, 3)[14:-2]
    (output / "header-12.fit").write_bytes(fit_packet(base, version=0x10, header_size=12))
    (output / "header-16.fit").write_bytes(fit_packet(base, version=0x10, header_size=16))
    (output / "header-zero.fit").write_bytes(fit_packet(base, version=0x10, zero=True))
    (output / "header-zero-data-only.fit").write_bytes(
        fit_packet(base, version=0x10, zero=True, only_data=True)
    )
    missing = definition(0, 20, [(253, 4, 0x86), (3, 1, 2)]) + struct.pack(
        "<BIB", 0, 1_160_000_100, 255
    )
    (output / "invalid-value.fit").write_bytes(fit_packet(missing))
    unknown = struct.pack(">BBBHB", 0x40, 0, 1, 0xFF01, 2)
    unknown += bytes([253, 4, 0x86, 99, 2, 0x84])
    unknown += struct.pack(">BIH", 0, 1_160_000_100, 0x1234)
    (output / "unknown-big-endian.fit").write_bytes(fit_packet(unknown))
    unknown_record = definition(0, 20, [(222, 2, 0x84)]) + struct.pack("<BH", 0, 0x1234)
    (output / "unknown-record-field.fit").write_bytes(fit_packet(unknown_record))
    timestamp = 1_160_000_126
    compressed = definition(0, 20, [(253, 4, 0x86), (3, 1, 2)])
    compressed += struct.pack("<BIB", 0, timestamp, 62)
    compressed += definition(1, 20, [(3, 1, 2)])
    compressed += bytes([0xA0 | ((timestamp + 5) & 31), 63])
    (output / "compressed-timestamp.fit").write_bytes(fit_packet(compressed))
    (output / "50k.fit").write_bytes(fit_member(0, 50_000, with_developer=True))
    if ceiling:
        wrapper, _ = zip_member(100 * 1024 * 1024)
        with ZipFile(io.BytesIO(wrapper)) as archive, archive.open(archive.infolist()[0]) as member:
            with (output / "100mib.fit").open("wb") as destination:
                while data := member.read(64 * 1024):
                    destination.write(data)


def observe(path):
    trace = dict(
        headers=0,
        definitions=0,
        crcs=0,
        messages=0,
        developer_fields=0,
        first=None,
        last=None,
        first_developer_description=None,
    )
    digest = hashlib.sha256()
    outcome = "decoded"
    try:
        with fitdecode.FitReader(path, check_crc=fitdecode.CrcCheck.RAISE) as reader:
            for frame in reader:
                if isinstance(frame, fitdecode.FitHeader):
                    trace["headers"] += 1
                elif isinstance(frame, fitdecode.FitDefinitionMessage):
                    trace["definitions"] += 1
                elif isinstance(frame, fitdecode.FitCRC):
                    trace["crcs"] += 1
                elif isinstance(frame, fitdecode.FitDataMessage):
                    trace["messages"] += 1
                    fields = []
                    for field in frame.fields:
                        index = getattr(field.field, "dev_data_index", None)
                        if index is not None:
                            trace["developer_fields"] += 1
                        fields.append((index, field.def_num, json_value(field.raw_value)))
                    fields.sort(key=lambda field: (-1 if field[0] is None else field[0], field[1]))
                    current = [frame.global_mesg_num, fields]
                    digest.update(
                        json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    digest.update(b"\n")
                    if trace["first"] is None:
                        trace["first"] = current
                    if (
                        frame.global_mesg_num == 206
                        and trace["first_developer_description"] is None
                    ):
                        trace["first_developer_description"] = current
                    trace["last"] = current
    except (fitdecode.FitError, EOFError, ValueError):
        outcome = "decode_error"
    if outcome == "decoded" and (not trace["headers"] or trace["headers"] != trace["crcs"]):
        outcome = "missing_crc"
    return {**trace, "outcome": outcome, "sha256": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    gen = subparsers.add_parser("create")
    gen.add_argument("path", type=Path)
    gen.add_argument("--ceiling", action="store_true")
    probe = subparsers.add_parser("observe")
    probe.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "create":
        create(args.path, ceiling=args.ceiling)
    else:
        print(json.dumps(observe(args.path)))


if __name__ == "__main__":
    main()
