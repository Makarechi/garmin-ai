"""Read-only Garmin boundary, with bounded retry and shared request pacing."""

import random
import time
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
from garmin_ai.garmin_contract import ENDPOINTS, AuthenticationRequired, CircuitOpen, Endpoint


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
        self._identity_invalid = False
        self.on_success = None

    def account_fingerprint(self):
        from garmin_ai.accounts import AccountError, profile_fingerprint

        with self.lock:
            if self._identity_invalid:
                raise AccountError("Authenticated stable profile identity unavailable")
            if self._account_fingerprint is None:
                profile = self._request(
                    self.client.connectapi, "/userprofile-service/socialProfile"
                )
                try:
                    self._account_fingerprint = profile_fingerprint(profile)
                except AccountError:
                    self._identity_invalid = True
                    raise
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
            return self._request(getattr(self.client, method), *args, **kwargs)

    def _request(self, request, *args, **kwargs):
        """Shared pacing and circuit handling; caller holds the reader lock."""
        if self.clock() < self.blocked_until:
            raise CircuitOpen("Garmin cooldown active")
        for attempt in range(self.attempts):
            self.sleep(max(0, self.next_request - self.clock()))
            self.next_request = self.clock() + self.interval
            try:
                result = request(*args, **kwargs)
                self.failures = 0
                if self.on_success is not None:
                    self.on_success()
                return result
            except GarminConnectAuthenticationError:
                self.blocked_until = self.clock() + 3600
                raise AuthenticationRequired("Garmin login must be renewed") from None
            except GarminConnectTooManyRequestsError as exc:
                self.blocked_until = self.clock() + 900
                exc.reader_cooldown_seconds = 900
                raise
            except GarminConnectConnectionError:
                self.failures += 1
                if self.failures >= 5:
                    self.blocked_until = self.clock() + 900
                    raise CircuitOpen("Repeated Garmin failures; retry after cooldown") from None
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
