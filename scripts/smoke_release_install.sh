#!/usr/bin/env bash
# Exercise a digest-pinned bundle on a disposable Docker host with fictional data.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
python_bin=${GA_SMOKE_PYTHON:-$root/.venv/bin/python}
architecture=$(uname -m)
runtime=${GA_SMOKE_RUNTIME:-python}
historical=${GA_SMOKE_HISTORICAL:-false}
dockerfile=Dockerfile
evidence=native-amd64-install.json
platform=linux/amd64
if [[ "$runtime" == rust ]]; then
    dockerfile=Dockerfile.rust
    evidence=native-rust-install.json
    case "$architecture" in
        aarch64|arm64) platform=linux/arm64 ;;
        x86_64) ;;
        *) echo "Unsupported host architecture." >&2; exit 2 ;;
    esac
    platform=${GA_SMOKE_PLATFORM:-$platform}
elif [[ "$runtime" != python ]]; then
    echo "Unknown installation smoke runtime." >&2
    exit 2
elif [[ "$architecture" != x86_64 && "${1:-}" != --allow-emulated ]]; then
    echo "Native amd64 smoke requires an x86_64 host." >&2
    exit 2
fi
if [[ "$historical" == true ]]; then
    if [[ "$runtime" != rust ]]; then
        echo "The historical upgrade smoke requires the native runtime." >&2
        exit 2
    fi
    evidence=native-rust-upgrade.json
fi
if [[ ! -x "$python_bin" ]]; then
    echo "Install the locked project environment before the smoke run." >&2
    exit 2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/garmin-release-smoke.XXXXXX")
registry_name="garmin-ai-native-smoke-$$"
instance="garmin-ci-$$"
image_tag=garmin-ai-install-smoke:$$
registry_tag=
bundle_dir=
cleanup() {
    exit_code=$?
    trap - EXIT
    if [[ -n "$bundle_dir" && -f "$bundle_dir/.env" ]]; then
        (
            cd "$bundle_dir"
            docker compose -p "$instance" --env-file .env --env-file release.env \
                -f compose.release.yml down -v --remove-orphans >/dev/null 2>&1
        ) || true
    fi
    docker stop "$registry_name" >/dev/null 2>&1 || true
    docker image rm "$image_tag" >/dev/null 2>&1 || true
    if [[ -n "$registry_tag" ]]; then
        docker image rm "$registry_tag" >/dev/null 2>&1 || true
    fi
    rm -r -- "$work"
    exit "$exit_code"
}
trap cleanup EXIT

docker buildx build --load --platform "$platform" -f "$root/$dockerfile" -t "$image_tag" "$root" >"$work/build.log" 2>&1 || {
    tail -40 "$work/build.log" >&2
    exit 1
}
image_ref=
if [[ "$runtime" == rust ]]; then
    image_ref=$(docker image inspect "$image_tag" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}')
fi
if [[ -z "$image_ref" ]] || ! docker image inspect "$image_ref" >/dev/null 2>&1; then
    docker run -d --rm -p 127.0.0.1::5000 --name "$registry_name" registry:2 >/dev/null
    registry_port=$(docker port "$registry_name" 5000/tcp | sed -n 's/.*://p')
    registry_tag="127.0.0.1:$registry_port/garmin-ai:smoke"
    docker tag "$image_tag" "$registry_tag"
    docker push "$registry_tag" >"$work/push.log" 2>&1 || {
        tail -40 "$work/push.log" >&2
        exit 1
    }
    digest=$(sed -n 's/.*digest: \(sha256:[a-f0-9]\{64\}\).*/\1/p' "$work/push.log" | tail -1)
    if [[ ! "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
        echo "Could not identify the registry image digest." >&2
        exit 1
    fi
    image_ref="127.0.0.1:$registry_port/garmin-ai@$digest"
fi
PYTHONPATH="$root/src" "$python_bin" "$root/scripts/build_release_bundle.py" \
    --runtime "$runtime" --app-image "$image_ref" --output "$work/bundle"
mkdir "$work/install"
tar -xzf "$work"/bundle/*.tar.gz -C "$work/install"
bundle_dir=$(find "$work/install" -mindepth 1 -maxdepth 1 -type d -print -quit)
if [[ -z "$bundle_dir" ]]; then
    echo "Release archive did not contain an installation directory." >&2
    exit 1
fi

cd "$bundle_dir"
compose() {
    docker compose -p "$instance" --env-file .env --env-file release.env -f compose.release.yml "$@"
}
ports=$("$python_bin" - <<'PY'
import socket

with socket.socket() as database, socket.socket() as api:
    for service in (database, api):
        service.bind(("127.0.0.1", 0))
    print(database.getsockname()[1], api.getsockname()[1])
PY
)
read -r db_port api_port <<< "$ports"
./install.sh setup --instance "$instance" --db-port "$db_port" --api-port "$api_port" \
    --locale en --timezone UTC --units metric
before=$(
    "$python_bin" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' .env
)
if [[ "$historical" == true ]]; then
    compose up -d --wait db
    PYTHONPATH="$root/src" "$python_bin" - "$root" "$instance" "$db_port" <<'PY'
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

root, instance, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
settings = dotenv_values(".env")
assert instance.startswith("garmin-ci-") and settings["COMPOSE_PROJECT_NAME"] == instance
url = make_url(settings["GA_DATABASE_URL"])
assert (url.host, url.port, url.database) == ("127.0.0.1", port, "garmin_ai")
engine = create_engine(url, hide_parameters=True)
with engine.begin() as connection:
    assert connection.execute(text("SELECT current_database(),current_schema()")).one() == (
        "garmin_ai",
        "public",
    )
    assert connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NULL"))
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/garmin_ai/migrations"))
    config.attributes["connection"] = connection
    command.upgrade(config, "f103aa712b44")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "f103aa712b44"
    connection.execute(text(
        "INSERT INTO app_state(key,value) VALUES ('synthetic:release-upgrade','{\"retained\": true}')"
    ))
engine.dispose()
print("Prepared the historical revision in the disposable installation database.")
PY
fi
./install.sh start
"$python_bin" "$root/scripts/verify_release_install.py" --env-file .env \
    --state-file "$work/tracker.json" --phase create --url "http://127.0.0.1:$api_port"
./install.sh stop
./install.sh setup --instance "$instance" --db-port "$db_port" --api-port "$api_port" \
    --locale en --timezone UTC --units metric
after=$(
    "$python_bin" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' .env
)
if [[ "$before" != "$after" ]]; then
    echo "Repeated setup changed private settings." >&2
    exit 1
fi
./install.sh start
"$python_bin" "$root/scripts/verify_release_install.py" --env-file .env \
    --state-file "$work/tracker.json" --phase check --url "http://127.0.0.1:$api_port"

if [[ "$runtime" == rust ]]; then
    if [[ "$historical" == true ]]; then
        compose exec -T db psql -X -U garmin -d garmin_ai -At -c \
            "SELECT current_database(),version_num,value->>'retained' FROM alembic_version CROSS JOIN app_state WHERE key='synthetic:release-upgrade'" \
            | grep -Fx 'garmin_ai|c54f8a72d130|true'
    fi
    for service in api worker; do
        compose exec -T "$service" sh -ec 'test "$(id -u)" -ne 0 && ! command -v python && ! command -v python3'
    done
    compose exec -T api sh -ec 'test -s /etc/ssl/certs/ca-certificates.crt && test -f /usr/share/zoneinfo/Europe/Bratislava'
    ./install.sh backup
    archive=$(find backups -maxdepth 1 -type f -name 'manual-*.enc' -print -quit)
    test -n "$archive"
    ./install.sh unpack-backup "${archive##*/}"
    recovered=$(find backups -mindepth 2 -name database.jsonl.gz -type f -print -quit)
    test -n "$recovered"
fi

mkdir -p "$root/test-results"
"$python_bin" - "$root/test-results/$evidence" "$architecture" \
    "$(git -C "$root" rev-parse HEAD)" "$image_ref" "$runtime" "$platform" "$historical" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "host_architecture": sys.argv[2],
            "native_amd64": sys.argv[2] == "x86_64" and sys.argv[6] == "linux/amd64",
            "git_sha": sys.argv[3],
            "app_image": sys.argv[4],
            "runtime": sys.argv[5],
            "platform": sys.argv[6],
            "setup_preserved_settings": True,
            "fictional_tracker_survived_restart": True,
            "native_backup_and_recovery_verified": sys.argv[5] == "rust",
            "historical_upgrade_verified": sys.argv[7] == "true",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
PY
echo "Release installation smoke passed on $architecture."
