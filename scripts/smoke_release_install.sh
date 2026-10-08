#!/usr/bin/env bash
# Exercise a digest-pinned bundle on a disposable Docker host with fictional data.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
python_bin=${GA_SMOKE_PYTHON:-$root/.venv/bin/python}
architecture=$(uname -m)
if [[ "$architecture" != x86_64 && "${1:-}" != --allow-emulated ]]; then
    echo "Native amd64 smoke requires an x86_64 host." >&2
    exit 2
fi
if [[ ! -x "$python_bin" ]]; then
    echo "Install the locked project environment before the smoke run." >&2
    exit 2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/garmin-release-smoke.XXXXXX")
registry_name="garmin-ai-native-smoke-$$"
instance="garmin-ci-$$"
image_tag=localhost:5099/garmin-ai:smoke
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
    rm -r -- "$work"
    exit "$exit_code"
}
trap cleanup EXIT

docker run -d --rm -p 127.0.0.1:5099:5000 --name "$registry_name" registry:2 >/dev/null
docker build --platform linux/amd64 -t "$image_tag" "$root" >"$work/build.log" 2>&1 || {
    tail -40 "$work/build.log" >&2
    exit 1
}
docker push "$image_tag" >"$work/push.log" 2>&1 || {
    tail -40 "$work/push.log" >&2
    exit 1
}
digest=$(sed -n 's/.*digest: \(sha256:[a-f0-9]\{64\}\).*/\1/p' "$work/push.log" | tail -1)
if [[ ! "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
    echo "Could not identify the registry image digest." >&2
    exit 1
fi
image_ref="localhost:5099/garmin-ai@$digest"
PYTHONPATH="$root/src" "$python_bin" "$root/scripts/build_release_bundle.py" \
    --app-image "$image_ref" --output "$work/bundle"
mkdir "$work/install"
tar -xzf "$work"/bundle/*.tar.gz -C "$work/install"
bundle_dir=$(find "$work/install" -mindepth 1 -maxdepth 1 -type d -print -quit)
if [[ -z "$bundle_dir" ]]; then
    echo "Release archive did not contain an installation directory." >&2
    exit 1
fi

cd "$bundle_dir"
./install.sh setup --instance "$instance" --db-port 55479 --api-port 18080 \
    --locale en --timezone UTC --units metric
before=$(
    "$python_bin" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' .env
)
./install.sh start
"$python_bin" "$root/scripts/verify_release_install.py" --env-file .env \
    --state-file "$work/tracker.json" --phase create --url http://127.0.0.1:18080
./install.sh stop
./install.sh setup --instance "$instance" --db-port 55479 --api-port 18080 \
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
    --state-file "$work/tracker.json" --phase check --url http://127.0.0.1:18080

mkdir -p "$root/test-results"
"$python_bin" - "$root/test-results/native-amd64-install.json" "$architecture" \
    "$(git -C "$root" rev-parse HEAD)" "$image_ref" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "host_architecture": sys.argv[2],
            "native_amd64": sys.argv[2] == "x86_64",
            "git_sha": sys.argv[3],
            "app_image": sys.argv[4],
            "setup_preserved_settings": True,
            "fictional_tracker_survived_restart": True,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
PY
echo "Release installation smoke passed on $architecture."
