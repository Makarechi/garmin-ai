#!/usr/bin/env bash
# Exercise a digest-pinned bundle on a disposable Docker host with fictional data.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
python_bin=${GA_SMOKE_PYTHON:-$root/.venv/bin/python}
architecture=$(uname -m)
runtime=${GA_SMOKE_RUNTIME:-python}
historical=${GA_SMOKE_HISTORICAL:-false}
gcp=${GA_SMOKE_GCP:-false}
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
profile=()
compose_files=(-f compose.release.yml)
if [[ "$gcp" == true ]]; then
    if [[ "$runtime" != rust || "$historical" == true ]]; then
        echo "The GCP smoke requires a clean native installation." >&2
        exit 2
    fi
    profile=(--gcp)
    compose_files+=(-f compose.gcp.yml)
    evidence=native-rust-gcp-install.json
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
                "${compose_files[@]}" down -v --remove-orphans >/dev/null 2>&1
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

revision=$(git -C "$root" rev-parse HEAD)
docker buildx build --load --platform "$platform" -f "$root/$dockerfile" \
    --label "org.opencontainers.image.revision=$revision" -t "$image_tag" "$root" >"$work/build.log" 2>&1 || {
    tail -40 "$work/build.log" >&2
    exit 1
}
if [[ "$runtime" == rust ]]; then
    docker run --rm --platform "$platform" --network none --read-only --cap-drop ALL \
        --entrypoint /usr/local/bin/garmin-ai-release-cleanup "$image_tag" --help >/dev/null
fi
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
if [[ "$runtime" == rust ]]; then
    ./install.sh "${profile[@]}" licenses >"$work/notices.txt"
    grep -Fxq 'Rust dependency license files bundled with this application image' "$work/notices.txt"
    docker run --rm --platform "$platform" --network none --read-only --entrypoint cat "$image_ref" \
        /usr/share/doc/garmin-ai/LICENSE-fitdecode.txt | cmp - "$root/crates/ga-fit/profile/LICENSE-fitdecode.txt"
    docker run --rm --platform "$platform" --network none --read-only --entrypoint cat "$image_ref" \
        /usr/share/doc/garmin-ai/rust-third-party-inventory.json | "$python_bin" -c '
import json
import sys

inventory = json.load(sys.stdin)
names = {crate["name"] for crate in inventory}
assert {"rmcp", "sqlx", "serde_json"} <= names
assert "pyo3" not in names
assert all(crate["license_files"] or crate["review_required"] for crate in inventory)
'
fi
compose() {
    docker compose -p "$instance" --env-file .env --env-file release.env "${compose_files[@]}" "$@"
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
./install.sh "${profile[@]}" setup --instance "$instance" --db-port "$db_port" --api-port "$api_port" \
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
    ./install.sh "${profile[@]}" migrate --status | "$python_bin" -c '
import json, sys
assert json.load(sys.stdin) == {"revision":"f103aa712b44","current":False}
'
    ./install.sh "${profile[@]}" migrate
    ./install.sh "${profile[@]}" migrate --status | "$python_bin" -c '
import json, sys
assert json.load(sys.stdin) == {"revision":"c54f8a72d130","current":True}
'
    PYTHONPATH="$root/scripts" "$python_bin" - "$api_port" "${profile[@]}" <<'PY'
import sys
from verify_release_install import inspect_bundle

inspect_bundle(f"http://127.0.0.1:{int(sys.argv[1])}", ["./install.sh", *sys.argv[2:]])
PY
    test -z "$(compose ps --status running --quiet api worker)"
fi
./install.sh "${profile[@]}" start
if [[ "$historical" == true ]]; then
    if ./install.sh "${profile[@]}" inspect >"$work/running-inspection.log" 2>&1; then
        echo "Inspection unexpectedly started while the API or worker was running." >&2
        exit 1
    fi
    grep -Fx 'Stop the API and worker before preparing a cutover.' "$work/running-inspection.log"
fi
"$python_bin" "$root/scripts/verify_release_install.py" --env-file .env \
    --state-file "$work/tracker.json" --phase create --url "http://127.0.0.1:$api_port"
./install.sh "${profile[@]}" stop
./install.sh "${profile[@]}" setup --instance "$instance" --db-port "$db_port" --api-port "$api_port" \
    --locale en --timezone UTC --units metric
after=$(
    "$python_bin" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' .env
)
if [[ "$before" != "$after" ]]; then
    echo "Repeated setup changed private settings." >&2
    exit 1
fi
./install.sh "${profile[@]}" start
"$python_bin" "$root/scripts/verify_release_install.py" --env-file .env \
    --state-file "$work/tracker.json" --phase check --url "http://127.0.0.1:$api_port"

if [[ "$runtime" == rust ]]; then
    if [[ "$historical" == true ]]; then
        compose exec -T db psql -X -U garmin -d garmin_ai -At -c \
            "SELECT current_database(),version_num,value->>'retained' FROM alembic_version CROSS JOIN app_state WHERE key='synthetic:release-upgrade'" \
            | grep -Fx 'garmin_ai|c54f8a72d130|true'
    fi
    if [[ "$gcp" == true ]]; then
        test "$(docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' "$(compose ps -q db)")" \
            = '335544320 805306368'
        test "$(docker inspect --format '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} {{index .HostConfig.LogConfig.Config "max-file"}}' "$(compose ps -q worker)")" \
            = 'json-file 5m 2'
    fi
    for service in api worker; do
        compose exec -T "$service" sh -ec '
            test "$(id -u)" -ne 0
            test "$(cat /proc/1/comm)" = garmin-ai-rust
            if command -v python >/dev/null || command -v python3 >/dev/null; then
                echo "Unexpected Python executable in the native image." >&2
                exit 1
            fi
            for command_file in /proc/[0-9]*/comm; do
                command_name=$(cat "$command_file" 2>/dev/null) || continue
                case "$command_name" in
                    python*|pypy*) echo "Unexpected Python process in the native runtime." >&2; exit 1 ;;
                esac
            done
        '
    done
    compose exec -T api sh -ec 'test -s /etc/ssl/certs/ca-certificates.crt && test -f /usr/share/zoneinfo/Europe/Bratislava'
    ./install.sh "${profile[@]}" backup
    archive=$(find backups -maxdepth 1 -type f -name 'manual-*.enc' -print -quit)
    test -n "$archive"
    ./install.sh "${profile[@]}" unpack-backup "${archive##*/}"
    recovered=$(find backups -mindepth 2 -name database.jsonl.gz -type f -print -quit)
    test -n "$recovered"

    compose stop api worker
    compose exec -T db psql -X -U garmin -d garmin_ai -At -c \
        "SELECT current_database(), EXISTS (SELECT FROM events)" | grep -Fx 'garmin_ai|t'
    compose run --rm --no-deps -T --entrypoint sh worker -ec '
        umask 077
        printf "synthetic file\n" > "$GA_DATA_DIR/erase-fixture"
        printf "synthetic token\n" > "$GA_TOKEN_DIR/erase-fixture"
    '
    archive_sha=$(
        "$python_bin" -c 'import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1],"rb"), "sha256").hexdigest())' "$archive"
    )
    for _ in 1 2; do
        compose run --rm --no-deps -T worker erase-all --confirm 'ERASE ALL LOCAL HEALTH DATA' \
            | "$python_bin" -c 'import json,sys; assert json.load(sys.stdin)["erased"] is True'
    done
    compose exec -T db psql -X -U garmin -d garmin_ai -At -c \
        "SELECT NOT EXISTS (SELECT FROM events), EXISTS (SELECT FROM app_state WHERE key='maintenance:erased')" \
        | grep -Fx 't|t'
    compose run --rm --no-deps -T --entrypoint sh worker -ec '
        test -z "$(ls -A "$GA_DATA_DIR")"
        test -z "$(ls -A "$GA_TOKEN_DIR")"
        test -f "$GA_LOCK_DIR/erased"
        test ! -e "$GA_LOCK_DIR/erasing"
    '
    compose run --rm --no-deps -T worker resume-storage | grep -Fx 'Storage re-enabled.'
    compose exec -T db psql -X -U garmin -d garmin_ai -At -c \
        "SELECT NOT EXISTS (SELECT FROM app_state WHERE key='maintenance:erased')" | grep -Fx t
    compose run --rm --no-deps -T --entrypoint sh worker -ec '
        test ! -e "$GA_LOCK_DIR/erased"
        test ! -e "$GA_LOCK_DIR/activating"
    '
    test "$archive_sha" = "$(
        "$python_bin" -c 'import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1],"rb"), "sha256").hexdigest())' "$archive"
    )"
fi

mkdir -p "$root/test-results"
"$python_bin" - "$root/test-results/$evidence" "$architecture" \
    "$revision" "$image_ref" "$runtime" "$platform" "$historical" "$gcp" <<'PY'
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
            "owner_onboarding_without_integrations_verified": True,
            "fictional_tracker_survived_restart": True,
            "fictional_tracker_entry_survived_preference_change": True,
            "native_runtime_processes_verified": sys.argv[5] == "rust",
            "native_backup_and_recovery_verified": sys.argv[5] == "rust",
            "native_mounted_erasure_verified": sys.argv[5] == "rust",
            "historical_upgrade_verified": sys.argv[7] == "true",
            "staged_read_only_upgrade_verified": sys.argv[7] == "true",
            "deployment_profile": "gcp" if sys.argv[8] == "true" else "standard",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
PY
echo "Release installation smoke passed on $architecture."
