"""Measure the Python voice limit with a synthetic provider timeout and authorized fallback."""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from garmin_ai.config import Settings
from garmin_ai.llm import GeminiProvider
from garmin_ai.runtime import VoiceTooLarge, transcribe_voice

MAX_AUDIO_BYTES = 20 * 1024 * 1024


class SyntheticFile:
    async def download_as_bytearray(self):
        return bytearray(MAX_AUDIO_BYTES)


class SyntheticBot:
    def __init__(self):
        self.downloads = 0

    async def get_file(self, _file_id):
        self.downloads += 1
        return SyntheticFile()


async def run(repetitions):
    calls = []
    bot = SyntheticBot()
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        gemini_api_key="synthetic",
        gemini_model="synthetic-primary",
        gemini_fallback_enabled=True,
        gemini_fallback_models=["synthetic-fallback"],
        llm_consent={
            "provider": "gemini",
            "model": "synthetic-primary",
            "fallback_models": ["synthetic-fallback"],
            "categories": ["audio"],
            "granted_at": datetime.now(UTC) - timedelta(seconds=1),
            "policy_revision": 1,
        },
    )

    def create(**kwargs):
        audio = kwargs["input"][0]
        if audio["mime_type"] != "audio/ogg" or len(audio["data"]) != 4 * (
            (MAX_AUDIO_BYTES + 2) // 3
        ):
            raise RuntimeError("Gemini adapter changed the exact size or mime type")
        calls.append({"model": kwargs["model"], "timeout_seconds": kwargs["timeout"]})
        if kwargs["model"] == "synthetic-primary":
            time.sleep(0.2)
            raise TimeoutError("synthetic provider deadline")
        return SimpleNamespace(status="completed", output_text='{"text":"synthetic voice note"}')

    client = SimpleNamespace(interactions=SimpleNamespace(create=create), close=lambda: None)
    with patch("garmin_ai.llm.genai", SimpleNamespace(Client=lambda **_kwargs: client)):
        provider = GeminiProvider(settings)
    start = time.monotonic()
    try:
        for _ in range(repetitions):
            transcript = await transcribe_voice(
                bot,
                provider,
                {"file_id": "synthetic", "duration": 600, "file_size": MAX_AUDIO_BYTES},
            )
            if transcript != "synthetic voice note":
                raise RuntimeError("Authorized fallback did not return the fixture")
        elapsed = time.monotonic() - start
        try:
            await transcribe_voice(
                bot,
                provider,
                {"file_id": "synthetic", "duration": 600, "file_size": MAX_AUDIO_BYTES + 1},
            )
        except VoiceTooLarge:
            pass
        else:
            raise RuntimeError("Oversized audio was not rejected")
    finally:
        provider.close()
    models = [row["model"] for row in calls]
    if (
        models != ["synthetic-primary", "synthetic-fallback"] * repetitions
        or bot.downloads != repetitions
    ):
        raise RuntimeError("Voice fallback or download sequencing changed")
    return {
        "audio_bytes_per_call": MAX_AUDIO_BYTES,
        "duration_seconds_per_clip": 600,
        "repetitions": repetitions,
        "downloads": bot.downloads,
        "provider_attempts": calls,
        "provider_models": models,
        "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
        "oversized_rejected_before_download": True,
        "elapsed_seconds": round(elapsed, 3),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repetitions", type=int, choices=(1, 2))
    options = parser.parse_args()
    print(json.dumps(asyncio.run(run(options.repetitions))))
