"""The separately shared declarative example stays data-only and importable."""

import json
from pathlib import Path

from garmin_ai.community_packs import CommunityPack


def test_declarative_pack_example_matches_public_contract():
    path = Path(__file__).resolve().parents[1] / "examples/declarative-pack/focus-walks.json"
    payload = json.loads(path.read_text())
    parsed = CommunityPack.model_validate(payload)
    assert parsed.key == "focus_walks" and parsed.version == 1
    assert all(
        not tracker.reminder_enabled and tracker.privacy == "sensitive"
        for tracker in parsed.trackers
    )
    assert not any(
        key in path.read_text().lower()
        for key in ("owner_id", "secret_refs", "destination_instance_id")
    )
