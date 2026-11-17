"""Write a two-member synthetic FIT archive through the baseline ingestion path."""

import argparse
import hashlib
import io
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fitdecode.utils import compute_crc
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.fit import store_fit
from garmin_ai.jobs import claim, enqueue, finish, renew
from garmin_ai.models import Activity, ActivityPart, SourcePayload

START = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
ACTIVITY = "synthetic-resource-fit"


def definition(local, global_number, fields, developer=None):
    head = 0x60 if developer else 0x40
    encoded = struct.pack("<BBBHB", head | local, 0, 0, global_number, len(fields))
    encoded += b"".join(bytes(field) for field in fields)
    if developer:
        encoded += bytes([1, *developer])
    return encoded


def fit_member(start_record, count, *, with_developer=False):
    # Record field 99 exercises the respiration field and its profile component.
    field_list = [(253, 4, 0x86), (3, 1, 0x02), (99, 2, 0x84)]
    timestamp = int((START - FIT_EPOCH).total_seconds())
    body = bytearray()
    if with_developer:
        # Encode the File ID, developer data ID and field description before the records.
        body.extend(definition(0, 0, [(0, 1, 0), (1, 2, 0x84), (4, 4, 0x86)]))
        body.extend(struct.pack("<BBHI", 0, 4, 255, timestamp))
        body.extend(definition(1, 207, [(1, 16, 0x0D), (3, 1, 2)]))
        body.extend(bytes([1]) + b"synthetic-fit-v1" + bytes([0]))
        body.extend(definition(2, 206, [(0, 1, 2), (1, 1, 2), (2, 1, 2), (3, 9, 7), (8, 6, 7)]))
        body.extend(bytes([2, 0, 1, 0x84]) + b"devscore\x00" + b"score\x00")
        body.extend(definition(3, 20, field_list, developer=(1, 2, 0)))
    else:
        body.extend(definition(0, 20, field_list))
    local = 3 if with_developer else 0
    for index in range(start_record, start_record + count):
        body.extend(struct.pack("<BIBH", local, timestamp + index, 60 + index % 60, index % 65000))
        if with_developer:
            body.extend(struct.pack("<H", 200 + index % 250))
    protocol = 0x20 if with_developer else 0x10
    header = struct.pack("<BBHI4s", 14, protocol, 2100, len(body), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    payload = header + body
    return payload + struct.pack("<H", compute_crc(payload))


def archive_input(records, *, with_developer=False):
    buffer = io.BytesIO()
    midpoint = records // 2
    members = [
        fit_member(0, midpoint, with_developer=with_developer),
        fit_member(midpoint, records - midpoint, with_developer=with_developer),
    ]
    with ZipFile(buffer, "w", compresslevel=6) as archive:
        for index, data in enumerate(members):
            entry = ZipInfo(f"member-{index}.fit")
            archive.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=6)
    return buffer.getvalue(), sum(map(len, members))


def store_phase(engine, archive, raw, lease_probe):
    def write():
        Path("/tmp/fit-projection-started").touch()
        with transaction(engine) as session:
            result = store_fit(session, archive, ACTIVITY, raw, fetched_at=START)
        Path("/tmp/fit-projection-store-finished").touch()
        return result

    if not lease_probe:
        return write(), []

    with transaction(engine) as session:
        now = datetime.now(UTC)
        scheduled = enqueue(
            session, "garmin_fit", {"activity_id": ACTIVITY}, f"fit:fixture:{ACTIVITY}", now
        )
        job = claim(session, kinds=["garmin_fit"], lease_seconds=30)
        if job is None or job.id != scheduled:
            raise RuntimeError("Could not reserve the synthetic FIT job")
        job_id, token = job.id, job.lease_token

    finished = Event()

    def renew_while_running():
        elapsed_ms = []
        while not finished.wait(0.25):
            started = time.monotonic()
            with transaction(engine) as session:
                if not renew(session, job_id, token, lease_seconds=30):
                    raise RuntimeError("The synthetic FIT lease was lost")
            elapsed_ms.append(round((time.monotonic() - started) * 1000, 3))
        return elapsed_ms

    with ThreadPoolExecutor(max_workers=1) as executor:
        background = executor.submit(renew_while_running)
        try:
            result = write()
        finally:
            finished.set()
        renew_times_ms = background.result()

    with transaction(engine) as session:
        finish(session, job_id, token)
    if not renew_times_ms:
        raise RuntimeError("The FIT workload ended before lease renewal")
    return result, renew_times_ms


def ingest_fit(records, *, with_developer=False, lease_probe=False):
    settings = Settings(_env_file=None)
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The FIT workload requires a dedicated synthetic test database")
    raw, uncompressed_size = archive_input(records, with_developer=with_developer)
    expected_messages = records + (6 if with_developer else 0)
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
        result, lease_renew_ms = store_phase(engine, archive, raw, lease_probe)
        elapsed = time.monotonic() - start
        if (result["status"], result["rows"]) != ("normalized", expected_messages):
            raise RuntimeError("The FIT workload failed to normalize or lost records")
        with transaction(engine) as session:
            parts = select(ActivityPart).where(ActivityPart.activity_id == ACTIVITY)
            count = session.scalar(select(func.count()).select_from(parts.subquery()))
            first_record = session.scalar(
                parts.where(ActivityPart.kind == "fit_record")
                .order_by(ActivityPart.sequence)
                .limit(1)
            )
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
            if count != expected_messages or last.sequence != expected_messages - 1:
                raise RuntimeError("Committed FIT sequence does not match the synthetic input")
            if source.payload_hash != source_digest or archive.read(source.archive_key) != raw:
                raise RuntimeError("The archived ZIP does not match the input bytes")
            fields = first_record.payload["_fit"]["fields"]
            if not any(field["field_number"] == 99 and field["raw_value"] == 0 for field in fields):
                raise RuntimeError("The raw respiration FIT field did not survive ingestion")
            if with_developer:
                dev = next(field for field in fields if field["developer"])
                if dev["name"] != "devscore" or dev["units"] != "score" or dev["raw_value"] != 200:
                    raise RuntimeError("The developer FIT field did not survive ingestion")
            return {
                "records": records,
                "stored_messages": count,
                "developer_fields": with_developer,
                "zip_size_bytes": len(raw),
                "fit_members": 2,
                "fit_size_bytes": uncompressed_size,
                "archive_sha256": source_digest,
                "projection_sha256": digest.hexdigest(),
                "parser_version": source.parser_version,
                "store_seconds": round(elapsed, 3),
                "lease_renew_ms": lease_renew_ms,
            }
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=int, choices=(5_000, 50_000), metavar="5000|50000")
    parser.add_argument("--developer", action="store_true")
    parser.add_argument("--lease-probe", action="store_true")
    options = parser.parse_args()
    print(
        json.dumps(
            ingest_fit(
                options.records, with_developer=options.developer, lease_probe=options.lease_probe
            )
        )
    )
