"""Exercise the exact uncompressed FIT archive admission limit with synthetic content."""

import argparse
import hashlib
import json
import struct
import tempfile
from datetime import timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fit_workload import ACTIVITY, START, definition
from fitdecode.utils import compute_crc

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.fit import MAX_FIT_BYTES, extract_fit, store_fit
from garmin_ai.models import Activity


def zip_member(size):
    """Make valid FIT framing while growing the on-disk member in small buffers."""
    header_bytes = 14
    crc_bytes = 2
    body_size = size - header_bytes - crc_bytes
    field_data = bytes(range(255))
    full_record = bytes([0]) + field_data
    descriptor = definition(0, 20, [(99, len(field_data), 0x0D)])
    first_record_count, remaining = divmod(body_size - len(descriptor) - 10, len(full_record))
    if not 1 <= remaining <= 255:
        raise ValueError("Fixture size does not leave room for a short tail field")
    tail = definition(1, 20, [(99, remaining, 0x0D)]) + bytes([1]) + field_data[:remaining]
    header = struct.pack("<BBHI4s", header_bytes, 0x20, 2100, body_size, b".FIT")
    header += struct.pack("<H", compute_crc(header))
    with tempfile.TemporaryFile() as temporary:
        with ZipFile(temporary, "w") as archive:
            item = ZipInfo("synthetic-limit.fit")
            item.compress_type = ZIP_DEFLATED
            with archive.open(item, "w") as member:
                member.write(header)
                member.write(descriptor)
                crc = compute_crc(descriptor, crc=compute_crc(header))
                repeat = 1024
                chunk = full_record * repeat
                blocks, rest = divmod(first_record_count, repeat)
                for _ in range(blocks):
                    member.write(chunk)
                    crc = compute_crc(chunk, crc=crc)
                if rest:
                    rest_bytes = full_record * rest
                    member.write(rest_bytes)
                    crc = compute_crc(rest_bytes, crc=crc)
                member.write(tail)
                crc = compute_crc(tail, crc=crc)
                member.write(struct.pack("<H", crc))
        temporary.seek(0)
        return temporary.read(), first_record_count + 1


def run(mib):
    requested = mib * 1024 * 1024
    if requested != MAX_FIT_BYTES:
        raise ValueError("This probe expects the published FIT input ceiling")
    settings = Settings(_env_file=None)
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The FIT ceiling probe requires a dedicated test database")
    raw, records = zip_member(requested)
    too_large, _ = zip_member(requested + 1)
    try:
        extract_fit(too_large)
    except ValueError:
        pass
    else:
        raise RuntimeError("An over-limit FIT archive was admitted")
    metadata = {
        "uncompressed_bytes": requested,
        "compressed_bytes": len(raw),
        "records": records,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "rejected_one_byte_over_limit": True,
    }
    Path("/tmp/fit-limit-input.json").write_text(json.dumps(metadata), encoding="utf-8")
    archive = LocalArchive(settings.data_dir / "raw")
    try:
        with transaction(engine) as session:
            session.add(
                Activity(
                    id=ACTIVITY,
                    kind="running",
                    start=START,
                    end=START + timedelta(minutes=1),
                    timezone="UTC",
                )
            )
        with transaction(engine) as session:
            result = store_fit(session, archive, ACTIVITY, raw, fetched_at=START)
        if (result["status"], result.get("rows")) != ("normalized", records):
            raise RuntimeError("The FIT ceiling case did not commit every decoded record")
        return {**metadata, "stored_rows": result["rows"]}
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mib", type=int, choices=(100,))
    options = parser.parse_args()
    print(json.dumps(run(options.mib)))
