"""Record Telegram ingress selection using pinned Python runtime configuration."""

import argparse
import json
from pathlib import Path

from garmin_ai.config import Settings
from garmin_ai.integrations import (
    channel_instance_id,
    configured_instance,
)


def capture(output):
    telegram = {"id": "channel:telegram:primary", "kind": "channel", "provider": "telegram"}
    secondary = {**telegram, "id": "channel:telegram:secondary"}
    unrelated = {"id": "channel:web:local", "kind": "channel", "provider": "web"}
    cases = [
        ("legacy", 42, None),
        ("empty_explicit_list", 42, []),
        ("no_private_chat", 0, []),
        ("omitted_telegram", 42, [unrelated]),
        ("explicit_disabled", 42, [{**telegram, "enabled": False}]),
        ("second_enabled", 42, [{**telegram, "enabled": False}, secondary]),
        ("first_enabled", 42, [secondary, telegram]),
        ("literal_instance", 42, [{**telegram, "id": "private"}]),
        ("compatible_boolean", 42, [{**telegram, "enabled": "no"}, secondary]),
        ("maximum_chat", 2**52 - 1, [secondary]),
    ]
    expected = []
    for name, owner, integrations in cases:
        supplied = {} if integrations is None else {"integrations": integrations}
        settings = Settings(
            _env_file=None, telegram_user_id=owner, telegram_bot_token="synthetic-bot", **supplied
        )
        polling = configured_instance(settings, "channel", "telegram") if owner else None
        expected.append(
            {
                "name": name,
                "owner": str(owner),
                "integrations": None if integrations is None else json.dumps(integrations),
                "namespace": None if polling is None else channel_instance_id(polling),
            }
        )
    output.write_text(json.dumps(expected), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
