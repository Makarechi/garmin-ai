"""Generate local defaults without displaying or overwriting existing secrets."""

import base64
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url


def main():
    path = Path(".env")
    values = dict(dotenv_values(path)) if path.exists() else {}
    defaults = {
        "GA_TIMEZONE": "Europe/Bratislava",
        "GA_DATA_DIR": "data",
        "GA_TOKEN_DIR": "tokens/garmin",
        "GA_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "GA_API_KEY": secrets.token_urlsafe(40),
        "GA_BACKUP_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "GA_APP_UID": str(os.getuid()),
        "GA_APP_GID": str(os.getgid()),
        "GA_LLM_ENABLED": "false",
        "GA_PROACTIVE_ENABLED": "false",
    }
    for key, value in defaults.items():
        values.setdefault(key, value)
    values.setdefault(
        "GA_DATABASE_URL",
        URL.create(
            "postgresql+psycopg",
            username="garmin",
            password=values["GA_POSTGRES_PASSWORD"],
            host="127.0.0.1",
            port=55432,
            database="garmin_ai",
        ).render_as_string(hide_password=False),
    )
    values.setdefault(
        "GA_CONTAINER_DATABASE_URL",
        make_url(values["GA_DATABASE_URL"])
        .set(host="db", port=5432)
        .render_as_string(hide_password=False),
    )
    existing = path.read_text() if path.exists() else ""
    additions = {k: v for k, v in values.items() if k not in dotenv_values(path)}
    # The project directory itself is not a secret directory and must retain its mode.
    content = existing.rstrip() + "\n" + "".join(f"{k}='{v}'\n" for k, v in additions.items())
    import tempfile

    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content.lstrip("\n"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    print("Local settings prepared. Existing values preserved; secrets were not printed.")


if __name__ == "__main__":
    main()
