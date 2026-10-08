"""Exercise a fictional tracker through an isolated release-bundle API."""

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

from dotenv import dotenv_values


def request(url: str, path: str, key: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url + path, data=data, headers=headers), timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status {response.status} for {path}")
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--phase", choices=("create", "check"), required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    key = dotenv_values(args.env_file)["GA_API_KEY"]
    if not key:
        raise RuntimeError("Release bundle API key is unavailable")
    request(args.url, "/health/ready", key)
    if args.phase == "create":
        draft = {
            "key": "ci_focus",
            "name": "CI focus",
            "locale": "en",
            "topology": "point",
            "fields": [
                {"key": "quality", "label": "Quality", "kind": "scale", "minimum": 1, "maximum": 5}
            ],
            "shortcut": "Log CI focus",
        }
        preview = request(args.url, "/tracker-setups/preview", key, draft)
        request(
            args.url,
            "/tracker-setups",
            key,
            {"draft": draft, "confirmation_token": preview["confirmation_token"]},
        )
    actions = request(args.url, "/actions", key)["actions"]
    matches = [action for action in actions if action["definition_key"] == "user.ci_focus"]
    if len(matches) != 1:
        raise RuntimeError("Fictional tracker action is missing or duplicated")
    if args.phase == "create":
        args.state_file.write_text(json.dumps({"action_id": matches[0]["id"]}) + "\n")
    elif matches[0]["id"] != json.loads(args.state_file.read_text())["action_id"]:
        raise RuntimeError("Fictional tracker changed identity after restart")
    print(f"Release API {args.phase} phase passed")


if __name__ == "__main__":
    main()
