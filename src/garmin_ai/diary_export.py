"""Bounded user-facing diary export, separate from administrative database backup."""

import csv
import io
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from garmin_ai.queries import list_events, time_range


def export_diary(session, start, end, timezone):
    time_range(start, end, 31)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown display timezone") from None
    data = list_events(session, start, end, limit=1000)
    if data["truncated"]:
        raise ValueError("More than 1000 diary records; narrow the export period")
    rows = []
    for event in data["rows"]:
        # Explicit allowlist excludes original Telegram text, inbox metadata,
        # idempotency keys, account identifiers and every operational table.
        row = {
            key: event[key]
            for key in (
                "id",
                "kind",
                "start",
                "end",
                "timezone",
                "source",
                "status",
                "confidence",
                "revision",
                "payload",
                "topology",
            )
        }
        row["display_start"] = datetime.fromisoformat(row["start"]).astimezone(zone).isoformat()
        row["display_end"] = (
            datetime.fromisoformat(row["end"]).astimezone(zone).isoformat() if row["end"] else None
        )
        row["missing_end"] = event["missing_end"]
        rows.append(row)
    result = {
        "format_version": 1,
        "start": start.astimezone(UTC).isoformat(),
        "end": end.astimezone(UTC).isoformat(),
        "display_timezone": timezone,
        "selection": "overlap_half_open; original intervals are not clipped",
        "missingness": "unreported_is_unknown",
        "rows": rows,
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 2_000_000:
        raise ValueError("Export exceeds 2 MB; narrow the period")
    return result


def csv_cell(value):
    text = "" if value is None else str(value)
    # Quotes alone do not prevent spreadsheet formula interpretation.
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
        else text
    )


def as_csv(data):
    stream = io.StringIO(newline="")
    columns = [
        "id",
        "kind",
        "display_start",
        "display_end",
        "display_timezone",
        "start",
        "end",
        "timezone",
        "topology",
        "missing_end",
        "status",
        "source",
        "confidence",
        "revision",
        "payload_json",
    ]
    writer = csv.DictWriter(stream, fieldnames=columns, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in data["rows"]:
        values = {
            **row,
            "display_timezone": data["display_timezone"],
            "payload_json": json.dumps(row["payload"], ensure_ascii=False, sort_keys=True),
        }
        writer.writerow({key: csv_cell(values.get(key)) for key in columns})
    return stream.getvalue()
