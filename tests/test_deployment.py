import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def configure(directory):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/configure.py")],
        cwd=directory,
        capture_output=True,
        text=True,
    )


def test_example_setup_creates_private_mounts_and_unique_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text((ROOT / ".env.example").read_text())
    mode = tmp_path.stat().st_mode
    assert configure(tmp_path).returncode == 0
    values = dotenv_values(path)
    assert not values["GA_API_KEY"].startswith("replace-with-")
    assert values["GA_BACKUP_KEY"]
    assert values["GA_GEMINI_THINKING_LEVEL"] == ""
    assert make_url(values["GA_DATABASE_URL"]).password == values["GA_POSTGRES_PASSWORD"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "tokens/garmin").is_dir()
    assert (tmp_path / ".state").stat().st_mode & 0o777 == 0o700
    assert configure(tmp_path).returncode == 0
    assert dotenv_values(path) == values
    assert tmp_path.stat().st_mode == mode


def test_setup_derives_missing_password_and_rejects_mismatch(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "GA_DATABASE_URL=postgresql+psycopg://garmin:custom-secret@127.0.0.1:55432/garmin_ai\n"
    )
    assert configure(tmp_path).returncode == 0
    assert dotenv_values(path)["GA_POSTGRES_PASSWORD"] == "custom-secret"
    before = path.read_text().replace("custom-secret", "other-secret", 1)
    path.write_text(before)
    assert configure(tmp_path).returncode != 0
    assert path.read_text() == before


def test_api_rejects_shipped_key(db_engine):
    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app
    from garmin_ai.config import Settings

    key = "replace-with-a-random-key-at-least-32-characters"
    with TestClient(create_app(Settings(api_key=key), db_engine)) as client:
        assert client.get("/tools", headers={"Authorization": "Bearer " + key}).status_code == 401


@pytest.mark.parametrize("username,database", [("other", "garmin_ai"), ("garmin", "other")])
def test_setup_rejects_identity_not_initialized_by_compose(tmp_path, username, database):
    path = tmp_path / ".env"
    before = (
        f"GA_DATABASE_URL=postgresql+psycopg://{username}:synthetic@127.0.0.1:55432/{database}\n"
    )
    path.write_text(before)
    result = configure(tmp_path)
    assert result.returncode != 0 and "Compose requires" in result.stderr
    assert path.read_text() == before


@pytest.mark.parametrize(
    "data,tokens", [("same", "same"), ("tokens/data", "tokens"), ("data", "data/tokens")]
)
def test_setup_rejects_overlapping_sources_without_changing_env(tmp_path, data, tokens):
    path = tmp_path / ".env"
    before = f"GA_DATA_DIR={data}\nGA_TOKEN_DIR={tokens}\n"
    path.write_text(before)
    result = configure(tmp_path)
    assert result.returncode != 0 and "must not overlap" in result.stderr
    assert path.read_text() == before


def test_setup_rejects_root_identity(tmp_path):
    path = tmp_path / ".env"
    before = "GA_APP_UID=0\nGA_APP_GID=0\n"
    path.write_text(before)
    result = configure(tmp_path)
    assert result.returncode != 0 and "non-root" in result.stderr
    assert path.read_text() == before


@pytest.mark.parametrize(
    "setting,endpoint",
    [
        ("GA_DATABASE_URL", "postgresql://garmin:synthetic@127.0.0.1:55432/garmin_ai"),
        ("GA_DATABASE_URL", "postgresql+psycopg://garmin:synthetic@other:55432/garmin_ai"),
        ("GA_DATABASE_URL", "postgresql+psycopg://garmin:synthetic@127.0.0.1:5432/garmin_ai"),
        ("GA_CONTAINER_DATABASE_URL", "postgresql://garmin:synthetic@db:5432/garmin_ai"),
        (
            "GA_CONTAINER_DATABASE_URL",
            "postgresql+psycopg://garmin:synthetic@127.0.0.1:5432/garmin_ai",
        ),
        ("GA_CONTAINER_DATABASE_URL", "postgresql+psycopg://garmin:synthetic@db:55432/garmin_ai"),
    ],
)
def test_setup_rejects_endpoints_outside_compose_topology(tmp_path, setting, endpoint):
    values = {
        "GA_DATABASE_URL": "postgresql+psycopg://garmin:synthetic@127.0.0.1:55432/garmin_ai",
        "GA_CONTAINER_DATABASE_URL": "postgresql+psycopg://garmin:synthetic@db:5432/garmin_ai",
    }
    values[setting] = endpoint
    before = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    path = tmp_path / ".env"
    path.write_text(before)
    result = configure(tmp_path)
    assert result.returncode != 0 and "database endpoint must use" in result.stderr
    assert path.read_text() == before


@pytest.mark.parametrize("source", ["GA_DATA_DIR", "GA_TOKEN_DIR"])
@pytest.mark.parametrize("directory", ["/data", "/mnt/data"])
def test_setup_rejects_source_roots_that_erasure_cannot_remove(tmp_path, source, directory):
    path = tmp_path / ".env"
    before = f"{source}={directory}\n"
    path.write_text(before)
    result = configure(tmp_path)
    assert result.returncode != 0 and "three levels below root" in result.stderr
    assert path.read_text() == before


def test_native_windows_setup_exits_before_uid_lookup(tmp_path):
    code = (
        "import runpy; ns=runpy.run_path("
        + repr(str(ROOT / "scripts/configure.py"))
        + "); ns['os'].name='nt'; ns['main']()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode != 0 and "Use WSL2" in result.stderr
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("key", ["human-readable-passphrase", "AAAA", "not%%%base64"])
def test_setup_rejects_invalid_preserved_backup_key_without_rewriting_env(tmp_path, key):
    path = tmp_path / ".env"
    original = f"GA_BACKUP_KEY={key}\n"
    path.write_text(original)
    result = configure(tmp_path)
    assert result.returncode != 0 and "GA_BACKUP_KEY" in result.stderr
    assert path.read_text() == original
    assert key not in result.stderr


def test_setup_preserves_valid_backup_key(tmp_path):
    import base64

    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    path = tmp_path / ".env"
    path.write_text(f"GA_BACKUP_KEY={key}\n")
    assert configure(tmp_path).returncode == 0
    assert dotenv_values(path)["GA_BACKUP_KEY"] == key


@pytest.mark.parametrize("size", [5, 31, 32])
def test_setup_validates_preserved_api_key(tmp_path, size):
    key = "x" * size
    path = tmp_path / ".env"
    original = f"GA_API_KEY={key}\n"
    path.write_text(original)
    result = configure(tmp_path)
    if size < 32:
        assert result.returncode != 0 and "GA_API_KEY" in result.stderr
        assert key not in result.stderr and path.read_text() == original
    else:
        assert result.returncode == 0 and dotenv_values(path)["GA_API_KEY"] == key


@pytest.mark.parametrize(
    "variable,host",
    [("GA_DATABASE_URL", "127.0.0.1:55432"), ("GA_CONTAINER_DATABASE_URL", "db:5432")],
)
@pytest.mark.parametrize(
    "option", ["host", "hostaddr", "port", "user", "password", "dbname", "service", "options"]
)
def test_setup_rejects_database_query_overrides(tmp_path, variable, host, option):
    path = tmp_path / ".env"
    before = f"GA_POSTGRES_PASSWORD=synthetic-secret\n{variable}=postgresql+psycopg://garmin:synthetic-secret@{host}/garmin_ai?{option}=override\n"
    path.write_text(before)
    result = configure(tmp_path)
    assert result.returncode != 0 and "query" in result.stderr
    assert path.read_text() == before
    assert "synthetic-secret" not in result.stderr and "override" not in result.stderr


def test_setup_preserves_allowed_database_query_options(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "GA_DATABASE_URL=postgresql+psycopg://garmin:synthetic-secret@127.0.0.1:55432/garmin_ai?connect_timeout=10&sslmode=prefer&application_name=garmin-ai\n"
    )
    assert configure(tmp_path).returncode == 0
    values = dotenv_values(path)
    assert (
        make_url(values["GA_DATABASE_URL"]).query
        == make_url(values["GA_CONTAINER_DATABASE_URL"]).query
    )


@pytest.mark.parametrize(
    "query",
    [
        "sslmode=required",
        "sslmode=require&sslmode=disable",
        "connect_timeout=ten",
        "connect_timeout=-1",
        "connect_timeout=2147483648",
        "application_name=bad%00name",
    ],
)
def test_setup_rejects_invalid_database_option_values(tmp_path, query):
    path = tmp_path / ".env"
    original = f"GA_DATABASE_URL=postgresql+psycopg://garmin:synthetic-secret@127.0.0.1:55432/garmin_ai?{query}\n"
    path.write_text(original)
    assert configure(tmp_path).returncode != 0
    assert path.read_text() == original


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_setup_rejects_tls_required_for_bundled_database(tmp_path, mode):
    path = tmp_path / ".env"
    original = f"GA_DATABASE_URL=postgresql+psycopg://garmin:synthetic-secret@127.0.0.1:55432/garmin_ai?sslmode={mode}\n"
    path.write_text(original)
    result = configure(tmp_path)
    assert result.returncode != 0 and "non-TLS" in result.stderr
    assert path.read_text() == original


@pytest.mark.parametrize("second", ["GA_TOKEN_DIR", "GA_BACKUP_DIR", "GA_LOCK_DIR"])
def test_setup_detects_case_aliases_in_storage_roots(tmp_path, second):
    source = tmp_path / "store" / "data"
    source.mkdir(parents=True)
    alias = tmp_path / "STORE" / "DATA"
    if not alias.exists() or not alias.samefile(source):
        pytest.skip("Requires a case-insensitive filesystem")
    source.chmod(0o750)
    path = tmp_path / ".env"
    original = f"GA_DATA_DIR={source}\n{second}={alias}/nested\n"
    path.write_text(original)
    assert configure(tmp_path).returncode != 0
    assert path.read_text() == original
    assert source.stat().st_mode & 0o777 == 0o750
