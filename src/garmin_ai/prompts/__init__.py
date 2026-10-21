"""Versioned, package-local instructions for model-facing boundaries."""

from functools import lru_cache
from importlib.resources import files
from typing import Literal, get_args

PromptName = Literal[
    "analysis_answer",
    "diary_extract",
    "form_safety",
    "mcp_server",
    "safety_fragment",
    "safety_reply",
    "tracker_extract",
    "voice_transcribe",
]


@lru_cache(maxsize=8)
def load_prompt(name: PromptName) -> str:
    """Load a bundled instruction without accepting arbitrary paths or user content."""

    if name not in get_args(PromptName):
        raise ValueError("Unknown model prompt")
    return files(__package__).joinpath(f"{name}.txt").read_text(encoding="utf-8").strip()
