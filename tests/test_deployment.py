import subprocess
import sys
from pathlib import Path

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
    assert make_url(values["GA_DATABASE_URL"]).password == values["GA_POSTGRES_PASSWORD"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "tokens/garmin").is_dir()
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
