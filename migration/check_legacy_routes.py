"""Capture read-only routes with an offline stand-in for the pinned Python Garmin SDK."""

import json
from datetime import date

from garminconnect import Garmin

from garmin_ai.garmin_contract import ENDPOINTS

DAY = date(2026, 11, 1).isoformat()
DISPLAY = "Synthetic Profile"
ACTIVITY_ID = "42"


def capture_routes():
    garmin = Garmin()
    garmin.display_name = DISPLAY
    captured = []

    def record(path, *, params=None):
        captured.append((path, {key: str(value) for key, value in (params or {}).items()}))
        return {"synthetic": True}

    garmin.connectapi = record
    garmin.download = record
    for endpoint in ENDPOINTS:
        args = {"day": (DAY,), "activity": (ACTIVITY_ID,), "global": ()}[endpoint.scope]
        getattr(garmin, endpoint.method)(*args)
        assert len(captured) == 1
        yield endpoint.name, captured.pop()
    garmin.get_activities(10, 100)
    assert len(captured) == 1
    yield "activities", captured.pop()
    garmin.download_activity(ACTIVITY_ID, garmin.ActivityDownloadFormat.ORIGINAL)
    assert len(captured) == 1
    yield "activity_fit", captured.pop()


if __name__ == "__main__":
    print(json.dumps(dict(capture_routes()), indent=2, sort_keys=True))
