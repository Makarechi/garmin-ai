#!/usr/bin/env python3
"""Emit non-sensitive release evidence for the exact checked-out revision."""

import json
import subprocess

from garmin_ai.operations import REVISION


def main():
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    print(
        json.dumps(
            {
                "git_sha": sha,
                "database_revision": REVISION,
                "test_environment": "disposable synthetic PostgreSQL/TimescaleDB",
                "live_services_used": False,
                "commands": [
                    "uv sync --locked --extra full",
                    "uv run ruff check .",
                    "uv run ruff format --check .",
                    "uv run pytest -q -ra --junitxml=test-results/pytest.xml",
                    "uv sync --locked",
                    "uv run pytest -q tests/test_integrations.py::test_core_cli_and_model_contract_import_without_optional_sdks tests/test_architecture_boundaries.py tests/test_release_gate.py",
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
