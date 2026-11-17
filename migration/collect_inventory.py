"""Inventory the Python baseline's public and persisted contracts without provider calls."""

import argparse
import ast
import inspect
import json
import os
import re
import subprocess
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError

from garmin_ai import api, cli, integrations, mcp_server, runtime
from garmin_ai.access import TOOL_SCOPES
from garmin_ai.config import Settings
from garmin_ai.definitions import _system_payload_models, system_registry_digest
from garmin_ai.garmin_contract import ENDPOINTS
from garmin_ai.metric_definitions import system_metric_registry_digest
from garmin_ai.metrics import CATALOG
from garmin_ai.models import Base
from garmin_ai.scenario_packs import PACKS
from garmin_ai.tools import TOOLS

SOURCE = Path("src/garmin_ai")


def source_tree(module):
    path = SOURCE / f"{module}.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def registered_routes():
    # Registration needs the settings and startup error path, but no database state.
    settings = Settings(_env_file=None)
    with patch.object(api, "transaction", side_effect=SQLAlchemyError("inventory")):
        app = api.create_app(settings, engine=object())
    rows = {}
    for route in app.routes:
        dependencies = []
        for dependency in route.dependant.dependencies:
            call = dependency.call
            if call.__name__ == "check":
                required = inspect.getclosurevars(call).nonlocals.get("required")
                if required is not None:
                    dependencies.extend(required)
                    continue
            if call.__name__ != "db":
                dependencies.append(call.__name__)
        for method in sorted(route.methods):
            rows[f"{method} {route.path}"] = {
                "handler": f"{route.endpoint.__module__}.{route.endpoint.__name__}",
                "declared_dependencies": dependencies,
                "in_openapi": route.include_in_schema,
                "status_code": route.status_code or 200,
            }
    return {
        "published_schema_route": app.openapi_url,
        "routes": rows,
        "openapi": app.openapi(),
    }


class ParserCaptured(Exception):
    def __init__(self, parser):
        self.parser = parser


def command_line():
    def capture(parser):
        raise ParserCaptured(parser)

    # Intercept parsing before main constructs settings or dispatches any command.
    with (
        patch.object(argparse.ArgumentParser, "parse_args", capture),
        patch("sys.argv", ["garmin-ai"]),
    ):
        try:
            cli.main()
        except ParserCaptured as captured:
            parser = captured.parser
        else:
            raise RuntimeError("CLI did not reach argument parsing")

    subcommands = next(
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = {}
    for name, subparser in subcommands.items():
        arguments = []
        for action in subparser._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            arguments.append(
                {
                    "name": action.dest,
                    "flags": action.option_strings,
                    "required": action.required,
                    "default": action.default,
                    "choices": list(action.choices) if action.choices is not None else None,
                    "hidden": action.help == argparse.SUPPRESS,
                }
            )
        commands[name] = {"help": subparser.format_help(), "arguments": arguments}

    cases = {}
    for name, args in {
        "help": ["--help"],
        "missing_command": [],
        "unknown_command": ["synthetic-unknown-command"],
        "missing_required_confirmation": ["erase-all"],
        "file_apply_without_preview_confirmation": [
            "import-file",
            "apply",
            "fake.csv",
            "fake.json",
        ],
        "inventory": ["inventory"],
    }.items():
        stdout, stderr = StringIO(), StringIO()
        with (
            patch("sys.argv", ["garmin-ai", *args]),
            patch.object(cli, "Settings", lambda: Settings(_env_file=None)),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                cli.main()
            except SystemExit as error:
                status = error.code
            else:
                status = 0
        cases[name] = {
            "argv": args,
            "exit": status,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
        }
    return {"commands": commands, "cases": cases}


def constant_strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return {
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    return set()


def job_kinds():
    result = set()
    for module in ("runtime", "sync"):
        for node in ast.walk(source_tree(module)):
            if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
                continue
            left = node.left
            if (
                isinstance(left, ast.Attribute)
                and left.attr == "kind"
                and isinstance(left.value, ast.Name)
                and left.value.id == "job"
            ):
                result.update(constant_strings(node.comparators[0]))
            if module == "sync" and isinstance(left, ast.Name) and left.id == "kind":
                result.update(constant_strings(node.comparators[0]))
    return sorted(result)


def telegram_surface():
    commands = set(runtime._LOCAL_CAPTION_COMMANDS)
    callbacks = set()
    for node in ast.walk(source_tree("telegram")):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id in {"command", "command_name"}:
                for value in node.comparators:
                    commands.update(
                        item for item in constant_strings(value) if item.startswith("/")
                    )
            if node.left.id == "callback":
                for value in node.comparators:
                    callbacks.update(constant_strings(value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "callback"
                and node.func.attr == "startswith"
            ):
                callbacks.update(constant_strings(node.args[0]))
        if isinstance(node, ast.keyword) and node.arg == "callback_data":
            callbacks.update(constant_strings(node.value))
    return {
        "commands": sorted(commands),
        "callbacks": sorted(callbacks),
        "dynamic_actions": {
            "action-token": "Instance-bound neutral reply action tokens",
            "create:": "Versioned tracker actions",
            "h:": "History selection and navigation tokens",
            "c:": "Caffeine recipe presets",
        },
    }


def web_commands():
    commands = set()
    for node in ast.walk(source_tree("web_chat")):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id == "text":
                for value in node.comparators:
                    commands.update(
                        item for item in constant_strings(value) if item.startswith("/")
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "text"
                and node.func.attr == "startswith"
            ):
                commands.update(
                    item.rstrip() for item in constant_strings(node.args[0]) if item.startswith("/")
                )
    return sorted(commands)


def integrations_inventory():
    registry = integrations.default_registry()
    metadata = {}
    for (kind, provider), descriptor in sorted(registry._factories.items()):
        metadata[f"{kind}:{provider}"] = {
            "capabilities": sorted(descriptor.capabilities),
            "required_modules": list(descriptor.required_modules),
        }
    for path in sorted(Path("examples").glob("*/pyproject.toml")):
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        for name, entry in (
            project.get("entry-points", {}).get(integrations.ENTRY_POINT_GROUP, {}).items()
        ):
            metadata[f"example:{project['name']}:{name}"] = {"entry_point": entry}
    return metadata


def build_inventory(baseline_sha):
    tracked = subprocess.check_output(["git", "ls-files", "-z"], text=True).split("\0")
    setup_files = [
        path
        for path in tracked
        if path == "install.sh"
        or path == "Dockerfile"
        or path.startswith(("compose", "scripts/", "deploy/", ".github/workflows/"))
    ]
    settings_keys = {f"GA_{name.upper()}" for name in Settings.model_fields}
    env_keys = set(settings_keys)
    key = re.compile(r"\bGA_[A-Z][A-Z0-9_]+\b")
    for path in [".env.example", *setup_files, *(str(path) for path in SOURCE.glob("*.py"))]:
        env_keys.update(key.findall(Path(path).read_text(encoding="utf-8")))

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    universal = json.loads(Path("tests/universal_acceptance_manifest.json").read_text())[
        "scenarios"
    ]
    agent = json.loads(Path("tests/evals/acceptance_cases.json").read_text())

    metric_extras = set()
    for node in ast.walk(source_tree("metric_definitions")):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "extras" for target in node.targets
        ):
            metric_extras.update(key.value for key in node.value.keys)

    return {
        "baseline_sha": baseline_sha,
        "http": registered_routes(),
        "cli": command_line(),
        "mcp": {
            "reads": {
                name: {
                    "schema": tool.arguments.model_json_schema(),
                    "http_declared_scopes": sorted(TOOL_SCOPES.get(name, ())),
                }
                for name, tool in sorted(TOOLS.items())
            },
            "opt_in_writes": {
                name: schema.model_json_schema()
                for name, (schema, _description) in sorted(mcp_server.WRITES.items())
            },
        },
        "job_kinds": job_kinds(),
        "telegram": telegram_surface(),
        "web_commands": web_commands(),
        "garmin": {
            **{entry.name: {"method": entry.method, "scope": entry.scope} for entry in ENDPOINTS},
            "activities": {"method": "get_activities", "scope": "page"},
            "activity_fit": {"method": "download_activity", "scope": "activity"},
        },
        "environment_keys": {
            name: "test_only"
            if name in {"GA_REQUIRE_TEST_DB", "GA_TEST_DATABASE_URL", "GA_SMOKE_PYTHON"}
            else "credential_reference_prefix"
            if name == "GA_PLUGIN_"
            else "app_setting"
            if name in settings_keys
            else "script_setting"
            for name in sorted(env_keys)
        },
        "settings_schema": Settings.model_json_schema(),
        "tables": sorted(set(Base.metadata.tables) | {"alembic_version"}),
        "schema_heads": script.get_heads(),
        "migration_revisions": {
            revision.revision: {
                "file": str(Path(revision.path).relative_to(Path.cwd())),
                "parent": revision.down_revision,
            }
            for revision in script.walk_revisions()
        },
        "system_event_types": sorted(_system_payload_models()),
        "system_event_digest": system_registry_digest(),
        "system_metrics": sorted(set(CATALOG) | metric_extras),
        "system_metric_digest": system_metric_registry_digest(),
        "system_packs": sorted(PACKS),
        "integration_entry_point": integrations.ENTRY_POINT_GROUP,
        "integrations": integrations_inventory(),
        "operational_files": setup_files,
        "universal_acceptance": {row["id"]: row["evidence"] for row in universal},
        "agent_acceptance": {row["id"]: row["name"] for row in agent},
    }


if __name__ == "__main__":
    baseline_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with patch.dict(os.environ, {}, clear=True):
        inventory = build_inventory(baseline_sha)
    print(json.dumps(inventory, ensure_ascii=False, indent=2, default=str, sort_keys=True))
