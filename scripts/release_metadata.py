#!/usr/bin/env python3
"""Emit non-sensitive release evidence for the exact checked-out revision."""

import json
import subprocess
from argparse import ArgumentParser

from garmin_ai.operations import REVISION

FULL_COMMANDS = [
    "uv sync --locked --extra full",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run pytest -q -ra --junitxml=test-results/pytest.xml --cov=garmin_ai --cov-branch --cov-report=term --cov-report=xml:test-results/coverage.xml --cov-report=html:test-results/htmlcov",
]
CORE_COMMANDS = [
    "uv sync --locked",
    "uv run pytest -q -ra --junitxml=test-results/core-only.xml tests/test_core_only_flow.py tests/test_integrations.py::test_core_cli_and_model_contract_import_without_optional_sdks tests/test_architecture_boundaries.py tests/test_release_gate.py",
]


def main():
    parser = ArgumentParser()
    parser.add_argument("--profile", choices=("full", "core-only"), default="full")
    profile = parser.parse_args().profile
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
                "profile": profile,
                "commands": FULL_COMMANDS if profile == "full" else CORE_COMMANDS,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
