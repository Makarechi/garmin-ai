"""Write a two-member synthetic FIT archive through the baseline ingestion path."""

import argparse
import hashlib
import io
import json
import struct
import time
from datetime import UTC, datetime, timedelta
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fitdecode.utils import compute_crc
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.fit import store_fit
from garmin_ai.models import Activity, ActivityPart, SourcePayload

START = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
ACTIVITY = "synthetic-resource-fit"


def fit_member(start_record, count):
    # Global message 20 is a record. Field 253 is its timestamp, 3 is heart rate,
    # and 99 is deliberately an unknown field with a uint16 raw value.
    body = bytearray(struct.pack("<BBBHB", 0x40, 0, 0, 20, 3))
    body.extend(bytes([253, 4, 0x86, 3, 1, 0x02, 99, 2, 0x84]))
    timestamp = int((START - FIT_EPOCH).total_seconds())
    for index in range(start_record, start_record + count):
        body.extend(struct.pack("<BIBH", 0, timestamp + index, 60 + index % 60, index % 65000))
    header = struct.pack("<BBHI4s", 14, 0x10, 2100, len(body), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    payload = header + body
    return payload + struct.pack("<H", compute_crc(payload))


def archive_input(records):
    buffer = io.BytesIO()
    midpoint = records // 2
    members = [fit_member(0, midpoint), fit_member(midpoint, records - midpoint)]
    with ZipFile(buffer, "w", compresslevel=6) as archive:
        for index, data in enumerate(members):
            entry = ZipInfo(f"member-{index}.fit")
            archive.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=6)
    return buffer.getvalue(), sum(map(len, members))


def ingest_fit(records):
    settings = Settings(_env_file=None)
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The FIT workload requires a dedicated synthetic test database")
    raw, uncompressed_size = archive_input(records)
    source_digest = hashlib.sha256(raw).hexdigest()
    archive = LocalArchive(settings.data_dir / "raw")

    try:
        with transaction(engine) as session:
            session.add(
                Activity(
                    id=ACTIVITY,
                    kind="running",
                    start=START,
                    end=START + timedelta(seconds=records),
                    timezone="UTC",
                )
            )
        start = time.monotonic()
        with transaction(engine) as session:
            result = store_fit(session, archive, ACTIVITY, raw, fetched_at=START)
        elapsed = time.monotonic() - start
        if (result["status"], result["rows"]) != ("normalized", records):
            raise RuntimeError("The FIT workload failed to normalize or lost records")
        with transaction(engine) as session:
            parts = select(ActivityPart).where(ActivityPart.activity_id == ACTIVITY)
            count = session.scalar(select(func.count()).select_from(parts.subquery()))
            first = session.scalar(parts.order_by(ActivityPart.sequence).limit(1))
            last = session.scalar(parts.order_by(ActivityPart.sequence.desc()).limit(1))
            source = session.scalar(
                select(SourcePayload).where(
                    SourcePayload.endpoint == "activity_fit", SourcePayload.source_key == ACTIVITY
                )
            )
            digest = hashlib.sha256()
            for row in session.scalars(
                parts.order_by(ActivityPart.sequence).execution_options(yield_per=250)
            ):
                encoded = json.dumps(
                    [row.kind, row.sequence, row.payload],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                digest.update(encoded.encode("utf-8") + b"\n")
            if count != records or (first.sequence, last.sequence) != (0, records - 1):
                raise RuntimeError("Committed FIT sequence does not match the synthetic input")
            if source.payload_hash != source_digest or archive.read(source.archive_key) != raw:
                raise RuntimeError("The archived ZIP does not match the input bytes")
            fields = first.payload["_fit"]["fields"]
            if not any(field["field_number"] == 99 and field["raw_value"] == 0 for field in fields):
                raise RuntimeError("The unknown FIT field did not survive ingestion")
            return {
                "records": count,
                "zip_size_bytes": len(raw),
                "fit_members": 2,
                "fit_size_bytes": uncompressed_size,
                "archive_sha256": source_digest,
                "projection_sha256": digest.hexdigest(),
                "parser_version": source.parser_version,
                "store_seconds": round(elapsed, 3),
            }
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=int, choices=(5_000, 50_000), metavar="5000|50000")
    options = parser.parse_args()
    print(json.dumps(ingest_fit(options.records)))
