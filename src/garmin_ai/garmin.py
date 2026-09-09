"""Read-only Garmin boundary, with bounded retry and shared request pacing."""

import random
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Lock

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_ai.archive import private_directory


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    scope: str = "day"
    target: str = "archive + normalize"


ENDPOINTS = (
    Endpoint("daily", "get_stats"),
    Endpoint("steps", "get_steps_data"),
    Endpoint("heart_rate", "get_heart_rates"),
    Endpoint("sleep", "get_sleep_data"),
    Endpoint("hrv", "get_hrv_data"),
    Endpoint("stress", "get_stress_data"),
    Endpoint("body_battery", "get_body_battery"),
    Endpoint("body_battery_events", "get_body_battery_events"),
    Endpoint("respiration", "get_respiration_data"),
    Endpoint("spo2", "get_spo2_data"),
    Endpoint("readiness", "get_training_readiness"),
    Endpoint("training_status", "get_training_status"),
    Endpoint("max_metrics", "get_max_metrics"),
    Endpoint("endurance", "get_endurance_score"),
    Endpoint("hill", "get_hill_score"),
    Endpoint("hydration", "get_hydration_data"),
    Endpoint("body_composition", "get_body_composition"),
    Endpoint("intensity", "get_intensity_minutes_data"),
    Endpoint("resting_hr", "get_rhr_day"),
    Endpoint("all_day_events", "get_all_day_events", target="archive; inspect schema"),
    Endpoint("devices", "get_devices", "global", "archive"),
    Endpoint("activity", "get_activity", "activity"),
    Endpoint("activity_details", "get_activity_details", "activity"),
    Endpoint("activity_splits", "get_activity_splits", "activity"),
    Endpoint("activity_typed_splits", "get_activity_typed_splits", "activity"),
    Endpoint("activity_zones", "get_activity_hr_in_timezones", "activity"),
    Endpoint("activity_weather", "get_activity_weather", "activity"),
)


class AuthenticationRequired(RuntimeError):
    pass


class CircuitOpen(RuntimeError):
    pass


class GarminReader:
    def __init__(self, client, *, interval=2.0, attempts=3, sleep=time.sleep, clock=time.monotonic):
        self.client = client
        self.interval = interval
        self.attempts = attempts
        self.sleep = sleep
        self.clock = clock
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.failures = 0
        self.lock = Lock()
        self._account_fingerprint = None

    def account_fingerprint(self):
        from garmin_ai.accounts import profile_fingerprint

        with self.lock:
            if self._account_fingerprint is None:
                self.sleep(max(0, self.next_request - self.clock()))
                self.next_request = self.clock() + self.interval
                try:
                    profile = self.client.connectapi("/userprofile-service/socialProfile")
                except GarminConnectAuthenticationError:
                    raise AuthenticationRequired("Garmin identity requires renewed login") from None
                self._account_fingerprint = profile_fingerprint(profile)
            return self._account_fingerprint

    @classmethod
    def restore(cls, token_dir: Path):
        client = Garmin()
        try:
            client.login(str(private_directory(token_dir).resolve()))
        except GarminConnectAuthenticationError:
            raise AuthenticationRequired("Run garmin-ai login in your local terminal") from None
        return cls(client)

    def call(self, method: str, *args, **kwargs):
        allowed = {e.method for e in ENDPOINTS} | {"get_activities", "download_activity"}
        if method not in allowed:
            raise ValueError("Method is not in the read-only endpoint registry")
        with self.lock:
            if self.clock() < self.blocked_until:
                raise CircuitOpen("Garmin cooldown active")
            for attempt in range(self.attempts):
                self.sleep(max(0, self.next_request - self.clock()))
                self.next_request = self.clock() + self.interval
                try:
                    result = getattr(self.client, method)(*args, **kwargs)
                    self.failures = 0
                    return result
                except GarminConnectAuthenticationError:
                    self.blocked_until = self.clock() + 3600
                    raise AuthenticationRequired("Garmin login must be renewed") from None
                except (GarminConnectConnectionError, GarminConnectTooManyRequestsError):
                    self.failures += 1
                    if self.failures >= 5:
                        self.blocked_until = self.clock() + 900
                        raise CircuitOpen(
                            "Repeated Garmin failures; retry after cooldown"
                        ) from None
                    if attempt + 1 == self.attempts:
                        raise
                    self.sleep(min(120, 5 * 2**attempt) + random.uniform(0, 2))

    def fetch(self, endpoint: Endpoint, day: date | None = None, activity_id: str | None = None):
        if endpoint.scope == "global":
            return self.call(endpoint.method)
        if endpoint.scope == "activity":
            if activity_id is None:
                raise ValueError("Activity ID required")
            return self.call(endpoint.method, activity_id)
        if day is None:
            raise ValueError("Date required")
        return self.call(endpoint.method, day.isoformat())
