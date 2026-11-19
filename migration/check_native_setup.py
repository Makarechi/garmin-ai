"""Run the pinned deployment setup tests against the native command."""

import argparse
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

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
    print("Verified native setup and repeat runs against the deployment contract")


if __name__ == "__main__":
    main()
