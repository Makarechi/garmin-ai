"""Run the pinned deployment setup tests against the native command."""

import argparse
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from garmin_ai.config import Settings

SELECTION = "test_setup or test_example_setup or test_invalid_instance or test_two_new_instances"


def command(binary, directory, *args):
    return subprocess.run(
        (binary, "configure", *args),
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


class SetupCommand:
    def __init__(self, binary):
        self.binary = binary

    def pytest_collection_modifyitems(self, items):
        for item in items:
            item.module.configure = self.configure

    def configure(self, directory, *args):
        return command(self.binary, directory, *args)


def preserved_bindings(binary):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        settings = root / ".env"
        current = (
            "# Preserve this comment and the existing multiline value.\n"
            "GA_EXAMPLE_MULTILINE='first line\nGA_EXAMPLE_MULTILINE_END=not-a-binding'\n"
            "GA_POSTGRES_PASSWORD='synthetic-%2F-parameter-@-post'\n"
            "GA_DATA_DIR='directory with spaces/data'\n"
        )
        settings.write_text(current)
        result = command(binary, root, "--instance", "fixture", "--api-port", "8087")
        assert result.returncode == 0, result.stderr
        assert "synthetic-%2F" not in result.stdout + result.stderr
        prepared = settings.read_text()
        assert prepared.startswith(current.split("GA_DATA_DIR", 1)[0])
        values = dotenv_values(settings)
        for option in ("GA_DATABASE_URL", "GA_CONTAINER_DATABASE_URL"):
            assert make_url(values[option]).password == values["GA_POSTGRES_PASSWORD"]
        assert Path(values["GA_DATA_DIR"]).samefile(root / "directory with spaces" / "data")
        assert settings.stat().st_mode & 0o777 == 0o600
        result = command(binary, root)
        assert result.returncode == 0, result.stderr
        assert settings.read_text() == prepared


def release_mounts(binary):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        settings = root / ".env"
        installation = "/installation/garmin/owner"
        current = (
            "GA_DATA_DIR='/local/private/owner/data'\n"
            "GA_TOKEN_DIR='/local/private/owner/tokens/garmin'\n"
            "GA_BACKUP_DIR='/backup/archive/owner/backups'\n"
            f"GA_LOCK_DIR='{installation}/state'\n"
        )
        settings.write_text(current)
        result = command(binary, root, "--mount-sources", installation)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["/local/private/owner", "/backup/archive/owner"]
        assert settings.read_text() == current and set(root.iterdir()) == {settings}
        settings.write_text("GA_DATA_DIR='/local/private/owner,invalid/data'\n")
        result = command(binary, root, "--mount-sources", installation)
        assert result.returncode != 0 and not result.stdout


def preserved_regional_credentials(binary):
    password = "p'ass$word-${SYNTHETIC}-replace-with-generated-password-real"
    api_key = "synthetic-owner-replace-with-existing-private-key"
    directory = "private' ${SYNTHETIC}/data"
    url = f"postgresql+psycopg://garmin:{quote(password, safe='')}@127.0.0.1:55432/garmin_ai"
    with TemporaryDirectory() as base:
        for locale in ("en-GB", "ru-RU", "zh-Hans"):
            root = Path(base) / locale
            root.mkdir()
            settings = root / ".env"
            settings.write_text(
                "SYNTHETIC=expanded\n"
                f"GA_DATABASE_URL={url}\n"
                f"GA_API_KEY='{api_key}'\n"
                f"GA_LOCALE='{locale}'\n"
                "GA_DATA_DIR='private\\' ${SYNTHETIC}/data'\n"
            )
            first = command(binary, root)
            assert first.returncode == 0, first.stderr
            assert password not in first.stdout + first.stderr
            expected = settings.read_bytes()
            actual = dotenv_values(settings, interpolate=False)
            assert actual["GA_LOCALE"] == locale and actual["GA_API_KEY"] == api_key
            for name in ("GA_DATABASE_URL", "GA_CONTAINER_DATABASE_URL"):
                assert make_url(actual[name]).password == password
            assert (root / directory).is_dir()
            second = command(binary, root)
            assert second.returncode == 0, second.stderr
            assert settings.read_bytes() == expected
            rejected = command(binary, root, "--instance", "a-different-installation")
            assert rejected.returncode != 0 and settings.read_bytes() == expected


def replace_runtime_placeholders(binary):
    placeholder = "replace-with-a-nonstandard-synthetic-placeholder"
    scoped_key = "synthetic-existing-scoped-key-setup-verification"
    for scoped in (None, scoped_key):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / ".env"
            current = f"GA_API_KEY='{placeholder}'\n"
            if scoped:
                tokens = json.dumps([{"key": scoped, "scopes": ["admin"]}])
                current += f"GA_API_TOKENS='{tokens}'\n"
            settings.write_text(current)
            result = command(binary, root)
            assert result.returncode == 0, result.stderr
            values = dotenv_values(settings, interpolate=False)
            if scoped:
                assert values["GA_API_KEY"] == "" and scoped in values["GA_API_TOKENS"]
            else:
                assert len(values["GA_API_KEY"]) >= 32
                assert not values["GA_API_KEY"].startswith("replace-with-")
            saved = settings.read_bytes()
            assert command(binary, root).returncode == 0
            assert settings.read_bytes() == saved


def reject_new_case_aliases(binary):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "case_probe").touch()
        if not (root / "CASE_PROBE").is_file():
            return False
        for index, aliases in enumerate(
            (
                ("store/data", "STORE/DATA", ".state", "backups"),
                ("store/data", "store/tokens", "STORE/DATA/state", "backups"),
                ("store/data", "store/tokens", ".state", "STORE/TOKENS/backups"),
            )
        ):
            case = root / str(index)
            case.mkdir()
            names = ("GA_DATA_DIR", "GA_TOKEN_DIR", "GA_LOCK_DIR", "GA_BACKUP_DIR")
            current = "".join(
                f"{name}='{alias}'\n" for name, alias in zip(names, aliases, strict=True)
            )
            settings = case / ".env"
            settings.write_text(current)
            result = command(binary, case)
            assert result.returncode != 0 and "directories" in result.stderr, result
            assert settings.read_text() == current
        return True


def validate_integrations_before_writes(binary):
    telegram = {"id": "channel:telegram:primary", "kind": "channel", "provider": "telegram"}
    model = {
        "id": "model:gemini:fixture",
        "kind": "model",
        "provider": "gemini",
        "enabled": "no",
        "request_timeout_seconds": "5.5",
        "config": {"name": "fake-model", "retry": 1, "online": True},
        "secret_refs": {"api_key": "GA_PLUGIN_SYNTHETIC_SETUP_KEY"},
    }
    valid = [telegram, model]
    rejected = [
        [{"kind": "channel", "provider": "telegram"}],
        [telegram, {**model, "id": telegram["id"]}],
        [telegram, {**model, "enabled": None}],
        [telegram, {**model, "config": {"private": ["synthetic-invalid-config"]}}],
        [telegram, {**model, "secret_refs": {"api_key": "GA_DATABASE_URL"}}],
        [{**telegram, "request_timeout_seconds": 1}],
        [{**telegram, "unrecognized": "synthetic-invalid-config"}],
    ]
    for entries in [*rejected, valid]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / ".env"
            current = (
                "GA_TELEGRAM_USER_ID=12345\n"
                "GA_TELEGRAM_BOT_TOKEN=1234:synthetic-fixture-telegram-token\n"
                f"GA_INTEGRATIONS='{json.dumps(entries)}'\n"
            )
            settings.write_text(current)
            result = command(binary, root)
            if entries == valid:
                assert Settings(_env_file=settings).telegram_user_id == 12345
                assert result.returncode == 0, result.stderr
                assert json.loads(dotenv_values(settings)["GA_INTEGRATIONS"]) == valid
            else:
                with pytest.raises(ValueError):
                    Settings(_env_file=settings)
                assert (
                    result.returncode != 0 and "Invalid preserved runtime settings" in result.stderr
                )
                assert settings.read_text() == current and set(root.iterdir()) == {settings}
                assert "synthetic" not in (result.stdout + result.stderr)
    print("Verified rejected integrations leave existing settings and storage untouched")


def validate_runtime_preferences(binary):
    preset = {
        "id": "673ac7ca-70f1-4a67-ad4c-705d9939af86",
        "name": "Synthetic drink",
        "recipe": {
            "beverage": "Synthetic recipe",
            "dose_basis": "total",
            "caffeine_mg_estimate": 5,
        },
    }
    other = {**preset, "id": "483b710b-0423-4edb-95ea-83eb98c476b0"}
    source = {
        "id": "cc5f37b3-c15a-4035-bbb8-70328a4261ac",
        "categories": ["work", "travel"],
        "granted_at": "2026-01-01T00:00:00Z",
    }
    malformed = [
        ("GA_BACKFILL_DAYS", "-1"),
        ("GA_BACKFILL_DAYS", "3661"),
        ("GA_BACKFILL_DAYS", "not-a-number"),
        ("GA_MODEL_REQUEST_TIMEOUT_SECONDS", "0.049"),
        ("GA_MODEL_REQUEST_TIMEOUT_SECONDS", "301"),
        ("GA_MODEL_REQUEST_TIMEOUT_SECONDS", "NaN"),
        ("GA_TELEGRAM_DISPATCHER_VERSION", "synthetic-unknown-dispatcher"),
        ("GA_GARMIN_AUTH_URL", "http://garmin.invalid/garmin-auth"),
        ("GA_GARMIN_AUTH_URL", "https://garmin.invalid/garmin-auth?context=test"),
        ("GA_GEMINI_FALLBACK_MODELS", json.dumps([""])),
        ("GA_GEMINI_FALLBACK_MODELS", json.dumps(["synthetic-test-model"] * 9)),
        ("GA_CALENDAR_SOURCES", json.dumps([{**source, "categories": ["synthetic"]}])),
        ("GA_CALENDAR_SOURCES", json.dumps([{**source, "granted_at": "2026-01-01T00:00:00"}])),
        ("GA_CAFFEINE_PRESETS", json.dumps([preset, preset])),
        ("GA_CAFFEINE_PRESETS", json.dumps([preset, other])),
        ("GA_CAFFEINE_PRESETS", json.dumps([{**preset, "recipe": {"beverage": ""}}])),
    ]
    for key, bad_value in malformed:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / ".env"
            current = f"{key}='{bad_value}'\n"
            settings.write_text(current)
            with pytest.raises(ValueError):
                Settings(_env_file=settings)
            result = command(binary, root)
            assert (
                result.returncode != 0 and "Invalid preserved runtime settings" in result.stderr
            ), key
            assert settings.read_text() == current and set(root.iterdir()) == {settings}, key

    for days, timeout in (("0", "0.05"), ("3660", "300")):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / ".env"
            choices = {
                "GA_BACKFILL_DAYS": days,
                "GA_MODEL_REQUEST_TIMEOUT_SECONDS": timeout,
                "GA_TELEGRAM_DISPATCHER_VERSION": "legacy-v1",
                "GA_GARMIN_AUTH_URL": "https://garmin.invalid/garmin-auth",
                "GA_GEMINI_FALLBACK_MODELS": json.dumps(["synthetic-test-model"]),
                "GA_CALENDAR_SOURCES": json.dumps([source]),
                "GA_CAFFEINE_PRESETS": json.dumps([preset]),
            }
            settings.write_text("".join(f"{key}='{value}'\n" for key, value in choices.items()))
            assert Settings(_env_file=settings).backfill_days == int(days)
            assert command(binary, root).returncode == 0
            prepared = dotenv_values(settings, interpolate=False)
            assert {key: prepared[key] for key in choices} == choices
    print("Verified preserved runtime preferences and exact backfill and timeout bounds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("legacy_checkout", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    tests = args.legacy_checkout.resolve(strict=True) / "tests" / "test_deployment.py"
    if (
        pytest.main(
            [str(tests), "-q", "-k", SELECTION, "-p", "no:cacheprovider"],
            plugins=[SetupCommand(binary)],
        )
        != pytest.ExitCode.OK
    ):
        raise SystemExit(1)
    preserved_bindings(binary)
    release_mounts(binary)
    preserved_regional_credentials(binary)
    replace_runtime_placeholders(binary)
    validate_integrations_before_writes(binary)
    validate_runtime_preferences(binary)
    if reject_new_case_aliases(binary):
        print("Verified overlapping storage paths whose case aliases did not exist before setup")
    print("Verified native setup and repeat runs against the deployment contract")


if __name__ == "__main__":
    main()
