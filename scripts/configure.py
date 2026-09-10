"""Generate local defaults without displaying or overwriting existing secrets."""

import base64
import json
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values, set_key
from pydantic import ValidationError
from sqlalchemy.engine import URL, make_url

from garmin_ai.config import Settings


class PreparedSettings(Settings):
    """Validate the file being prepared without ambient environment overrides."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, **sources):
        return (sources["init_settings"],)


def main():
    if os.name == "nt":
        raise SystemExit(
            "Use WSL2 or Linux to configure this Docker deployment; native Windows setup is unsupported."
        )
    path = Path(".env")
    values = dict(dotenv_values(path)) if path.exists() else {}
    try:
        api_tokens = json.loads(values.get("GA_API_TOKENS") or "[]")
    except (ValueError, TypeError):
        raise ValueError(
            "Invalid preserved runtime settings; existing settings were not changed"
        ) from None
    defaults = {
        "GA_TIMEZONE": "Europe/Bratislava",
        "GA_DATA_DIR": "data",
        "GA_BACKUP_DIR": "backups",
        "GA_LOCK_DIR": ".state",
        "GA_TOKEN_DIR": "tokens/garmin",
        "GA_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "GA_API_KEY": "" if api_tokens else secrets.token_urlsafe(40),
        "GA_BACKUP_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "GA_APP_UID": str(os.getuid()),
        "GA_APP_GID": str(os.getgid()),
        "GA_LLM_ENABLED": "false",
        "GA_PROACTIVE_ENABLED": "false",
    }
    original = dict(values)
    for key, value in defaults.items():
        if key == "GA_API_KEY" and values.get(key) == "" and api_tokens:
            continue
        if not values.get(key) or values[key].startswith("replace-with-"):
            values[key] = value
    try:
        ZoneInfo(values["GA_TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            "GA_TIMEZONE must name a valid IANA timezone; existing settings were not changed"
        ) from None
    if values["GA_API_KEY"] and len(values["GA_API_KEY"]) < 32:
        raise ValueError(
            "GA_API_KEY must contain at least 32 characters; existing settings were not changed"
        )
    try:
        decoded_backup_key = base64.b64decode(
            values["GA_BACKUP_KEY"], altchars=b"-_", validate=True
        )
    except (ValueError, TypeError):
        raise ValueError(
            "GA_BACKUP_KEY must be Base64 encoding of exactly 32 bytes; existing settings were not changed"
        ) from None
    if len(decoded_backup_key) != 32:
        raise ValueError(
            "GA_BACKUP_KEY must encode exactly 32 bytes; existing settings were not changed"
        )
    for key in ("GA_DATABASE_URL", "GA_CONTAINER_DATABASE_URL"):
        if not values.get(key) or "replace-with-generated-password" in values[key]:
            values.pop(key, None)
    database = make_url(values["GA_DATABASE_URL"]) if values.get("GA_DATABASE_URL") else None
    if database:
        if not original.get("GA_POSTGRES_PASSWORD") or original["GA_POSTGRES_PASSWORD"].startswith(
            "replace-with-"
        ):
            if not database.password:
                raise ValueError("Configured database URL requires a password")
            values["GA_POSTGRES_PASSWORD"] = database.password
        elif database.password != values["GA_POSTGRES_PASSWORD"]:
            raise ValueError(
                "Database URL and GA_POSTGRES_PASSWORD disagree; existing values were not changed"
            )
    else:
        database = URL.create(
            "postgresql+psycopg",
            username="garmin",
            password=values["GA_POSTGRES_PASSWORD"],
            host="127.0.0.1",
            port=55432,
            database="garmin_ai",
        )
        values["GA_DATABASE_URL"] = database.render_as_string(hide_password=False)
    if values.get("GA_CONTAINER_DATABASE_URL"):
        container = make_url(values["GA_CONTAINER_DATABASE_URL"])
        if (container.username, container.password, container.database) != (
            database.username,
            database.password,
            database.database,
        ):
            raise ValueError(
                "Host and container database credentials disagree; existing values were not changed"
            )
    else:
        values["GA_CONTAINER_DATABASE_URL"] = database.set(host="db", port=5432).render_as_string(
            hide_password=False
        )
    container = make_url(values["GA_CONTAINER_DATABASE_URL"])
    allowed_connection_options = {"sslmode", "connect_timeout", "application_name"}
    if any(set(url.query) - allowed_connection_options for url in (database, container)):
        raise ValueError(
            "Database URL query may only contain sslmode, connect_timeout and application_name"
        )
    for url in (database, container):
        for option, value in url.query.items():
            if not isinstance(value, str):
                raise ValueError("Database query options must occur only once")
            if option == "sslmode" and value not in {
                "disable",
                "allow",
                "prefer",
            }:
                raise ValueError(
                    "Bundled database sslmode must allow non-TLS connections: disable, allow or prefer"
                )
            if option == "connect_timeout" and (
                not value.isascii() or not value.isdecimal() or not 0 <= int(value) <= 2147483647
            ):
                raise ValueError("Database connect_timeout must be a nonnegative 32-bit integer")
            if option == "application_name" and ("\x00" in value or len(value.encode()) > 63):
                raise ValueError(
                    "Database application_name must be at most 63 bytes and contain no NUL"
                )
    if (database.drivername, database.host, database.port) != (
        "postgresql+psycopg",
        "127.0.0.1",
        55432,
    ):
        raise ValueError("Host database endpoint must use postgresql+psycopg at 127.0.0.1:55432")
    if (container.drivername, container.host, container.port) != ("postgresql+psycopg", "db", 5432):
        raise ValueError("Container database endpoint must use postgresql+psycopg at db:5432")
    if (database.username, database.database) != ("garmin", "garmin_ai"):
        raise ValueError("Compose requires database user garmin and database garmin_ai")
    if int(values["GA_APP_UID"]) <= 0 or int(values["GA_APP_GID"]) < 0:
        raise ValueError("Configure a non-root service UID and a valid GID")
    data = Path(values["GA_DATA_DIR"]).expanduser().resolve()
    tokens = Path(values["GA_TOKEN_DIR"]).expanduser().resolve()
    if data.is_relative_to(tokens) or tokens.is_relative_to(data):
        raise ValueError("Data and token directories must not overlap")
    prepared = {
        key.removeprefix("GA_").lower(): value
        for key, value in values.items()
        if key.startswith("GA_")
    }
    for key in ("data_dir", "token_dir", "backup_dir", "lock_dir"):
        prepared[key] = Path(prepared[key]).expanduser().resolve()
    try:
        prepared["api_tokens"] = api_tokens
        PreparedSettings(**prepared)
    except ValidationError:
        # Pydantic errors can include the original input, including secrets.
        raise ValueError(
            "Invalid preserved runtime settings; existing settings were not changed"
        ) from None
    # Bind mounts must exist and be owned by the configured service user.
    for key in ("GA_DATA_DIR", "GA_TOKEN_DIR", "GA_BACKUP_DIR", "GA_LOCK_DIR"):
        directory = Path(values[key]).expanduser().resolve()
        if key in {"GA_DATA_DIR", "GA_TOKEN_DIR"} and len(directory.parts) < 4:
            raise ValueError("Use a dedicated source directory at least three levels below root")
        if (
            directory == Path.cwd()
            or directory in Path.cwd().parents
            or directory == Path.home()
            or (
                directory.exists()
                and any(
                    directory.samefile(protected)
                    for protected in (Path.home(), Path.cwd(), *Path.cwd().parents)
                )
            )
        ):
            raise ValueError("Use a dedicated private storage directory")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != int(values["GA_APP_UID"]) or info.st_gid != int(values["GA_APP_GID"]):
            raise ValueError("Storage ownership must match GA_APP_UID and GA_APP_GID")
        values[key] = str(directory)

    def contains(parent, child):
        return any(parent.samefile(ancestor) for ancestor in (child, *child.parents))

    data, tokens = Path(values["GA_DATA_DIR"]), Path(values["GA_TOKEN_DIR"])
    if contains(data, tokens) or contains(tokens, data):
        raise ValueError("Data and token directories must not overlap")
    if any(
        contains(Path(values[key]), Path(values["GA_BACKUP_DIR"]))
        for key in ("GA_DATA_DIR", "GA_TOKEN_DIR")
    ):
        raise ValueError("Backups must be outside private source directories")
    if any(
        contains(Path(values[key]), Path(values["GA_LOCK_DIR"]))
        for key in ("GA_DATA_DIR", "GA_TOKEN_DIR")
    ):
        raise ValueError("Lock directory must be outside private source directories")
    for key in ("GA_DATA_DIR", "GA_TOKEN_DIR", "GA_BACKUP_DIR", "GA_LOCK_DIR"):
        Path(values[key]).chmod(0o700)
    existing = path.read_text() if path.exists() else ""
    import tempfile

    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(existing)
        for key, value in values.items():
            if original.get(key) != value:
                set_key(name, key, value or "", quote_mode="always")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    print("Local settings prepared. Existing configured secrets preserved; placeholders replaced.")


if __name__ == "__main__":
    main()
