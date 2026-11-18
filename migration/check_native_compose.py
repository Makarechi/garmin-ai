"""Exercise native Compose startup and restart with fresh disposable volumes."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


def run(image, platform, legacy_image=None):
    project = f"garmin-rust-compose-test-{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(32)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GA_")}
    environment.update(
        GA_APP_IMAGE=image,
        GA_POSTGRES_PASSWORD=password,
        GA_CONTAINER_DATABASE_URL=f"postgresql+psycopg://garmin:{password}@db:5432/garmin_ai",
        GA_API_PORT="0",
        GA_WORKER_ENV_FILE="/dev/null",
        DOCKER_DEFAULT_PLATFORM=platform,
    )
    prefix = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "--project-name",
        project,
        "--file",
        str(ROOT / "compose.rust.yml"),
    ]

    def compose(*arguments, expect=0):
        output = subprocess.run(
            [*prefix, *arguments],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=130,
        )
        assert output.returncode == expect, (
            arguments[0],
            output.stderr.replace(password, "<redacted>"),
        )
        return output.stdout.strip()

    try:
        compose("config", "--quiet")
        compose("pull", "--policy", "always", "db")
        if legacy_image:
            compose("up", "--detach", "--wait", "--wait-timeout", "100", "db")
            legacy_migration = """
                IFS= read -r ga_fixture_secret
                export GA_DATABASE_URL="postgresql+psycopg://garmin:$ga_fixture_secret@db:5432/garmin_ai"
                unset ga_fixture_secret
                exec /app/.venv/bin/alembic upgrade f103aa712b44
            """
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    "--network",
                    f"{project}_default",
                    "--workdir",
                    "/app",
                    "--entrypoint",
                    "sh",
                    legacy_image,
                    "-ec",
                    legacy_migration,
                ],
                input=password + "\n",
                capture_output=True,
                text=True,
                timeout=100,
            )
            assert result.returncode == 0, result.stderr.replace(password, "<redacted>")
            installed = compose(
                "exec",
                "-T",
                "db",
                "psql",
                "-XAt",
                "-U",
                "garmin",
                "-d",
                "garmin_ai",
                "-c",
                "SELECT version_num FROM alembic_version",
            )
            assert installed == "f103aa712b44"
        compose("up", "--detach", "--wait", "--wait-timeout", "100")
        bind_host, bind_port = compose("port", "api", "8080").rsplit(":", 1)
        assert bind_host == "127.0.0.1"
        for path in ("live", "ready"):
            with urlopen(f"http://127.0.0.1:{int(bind_port)}/health/{path}", timeout=3) as result:
                assert result.status == 200
        status = compose("exec", "-T", "api", "garmin-ai-rust", "migrate", "--status")
        assert json.loads(status) == {"revision": "c54f8a72d130", "current": True}
        for service in ("api", "worker"):
            assert compose("exec", "-T", service, "id", "-u") == "1000"
        compose("exec", "-T", "api", "garmin-ai-rust", "healthcheck", "--worker")
        compose("stop", "--timeout", "5", "worker")
        compose("exec", "-T", "api", "garmin-ai-rust", "healthcheck", "--worker", expect=1)
        compose("up", "--detach", "--wait", "--wait-timeout", "100", "worker")
        compose("exec", "-T", "api", "garmin-ai-rust", "healthcheck", "--worker")
        compose("exec", "-T", "api", "garmin-ai-rust", "healthcheck", "--ready")
    finally:
        compose("down", "--volumes", "--timeout", "5")
    mode = "upgrade from f103aa712b44" if legacy_image else "empty installation"
    print(f"Verified {platform} native Compose {mode}, probes and worker restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--platform", choices=("linux/arm64", "linux/amd64"), required=True)
    parser.add_argument("--legacy-image")
    args = parser.parse_args()
    run(args.image, args.platform, args.legacy_image)
