"""Capture source-instance selection in the pinned Python configuration."""

import argparse
import json
import tempfile
from pathlib import Path

from garmin_ai.config import Settings
from garmin_ai.integrations import configured_instance


def capture(output):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        saved = root / "saved"
        saved.mkdir(mode=0o700)
        (saved / "garmin_tokens.json").write_text("synthetic token file", encoding="utf-8")
        source = {"id": "source:garmin:archive", "kind": "source", "provider": "garmin"}
        cases = [
            ("no_source", False, None),
            ("legacy", True, None),
            ("empty_allowlist", True, []),
            ("explicit_waiting_for_login", False, [source]),
            ("explicit_disabled", True, [{**source, "enabled": False}]),
            (
                "first_enabled",
                True,
                [
                    {**source, "id": "source:garmin:disabled", "enabled": False},
                    source,
                    {**source, "id": "source:garmin:other"},
                ],
            ),
        ]
        records = []
        for name, has_tokens, integrations in cases:
            settings = Settings(
                _env_file=None,
                token_dir=saved if has_tokens else root / "absent",
                **({} if integrations is None else {"integrations": integrations}),
            )
            selected = configured_instance(settings, "source", "garmin")
            records.append(
                {
                    "name": name,
                    "has_tokens": has_tokens,
                    "integrations": None if integrations is None else json.dumps(integrations),
                    "selected": selected.id if selected else None,
                }
            )
        output.write_text(json.dumps(records), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
