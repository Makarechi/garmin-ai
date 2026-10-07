#!/usr/bin/env bash
# Run a digest-pinned, source-free installation bundle with Docker only.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Docker with the Compose plugin is required." >&2
    exit 1
fi
compose_version=$(docker compose version --short)
compose_version=${compose_version#v}
compose_major=${compose_version%%.*}
compose_minor=${compose_version#*.}
compose_minor=${compose_minor%%.*}
case "$compose_major$compose_minor" in
    ''|*[!0-9]*) echo "Docker Compose 2.24.0 or newer is required." >&2; exit 1 ;;
esac
if [ "$compose_major" -lt 2 ] || { [ "$compose_major" -eq 2 ] && [ "$compose_minor" -lt 24 ]; }; then
    echo "Docker Compose 2.24.0 or newer is required." >&2
    exit 1
fi

image=$(sed -n 's/^GA_APP_IMAGE=//p' release.env)
if ! printf '%s\n' "$image" | grep -Eq '^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$'; then
    echo "release.env must contain one digest-pinned GA_APP_IMAGE." >&2
    exit 1
fi

compose() {
    docker compose --env-file .env --env-file release.env -f compose.release.yml "$@"
}

case "${1:-}" in
    demo)
        shift
        exec docker run --rm --read-only \
            --user "$(id -u):$(id -g)" \
            -p 127.0.0.1:8765:8765 \
            --mount "type=bind,source=$PWD,target=/setup,readonly" \
            --entrypoint python "$image" \
            /setup/scripts/serve_demo.py --host 0.0.0.0 --port 8765 "$@"
        ;;
    setup)
        shift
        mount_list=$(docker run --rm --read-only \
            --user "$(id -u):$(id -g)" \
            --mount "type=bind,source=$PWD,target=/setup,readonly" \
            --entrypoint python "$image" \
            /setup/scripts/release_mounts.py /setup/.env "$PWD")
        mount_args=(--mount "type=bind,source=$PWD,target=$PWD")
        while IFS= read -r directory; do
            if [ -n "$directory" ]; then
                mount_args+=(--mount "type=bind,source=$directory,target=$directory")
            fi
        done <<< "$mount_list"
        docker run --rm \
            --user "$(id -u):$(id -g)" \
            "${mount_args[@]}" --workdir "$PWD" --entrypoint python "$image" \
            "$PWD/scripts/configure.py" "$@"
        compose config --quiet
        ;;
    start)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose config --quiet
        compose up -d --wait
        ;;
    status)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose ps
        ;;
    stop)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose stop
        ;;
    backup)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose config --quiet
        compose stop worker
        backup_name="manual-$(date -u +%Y%m%dT%H%M%SZ)-$$.enc"
        compose run --rm --no-deps worker garmin-ai backup "/app/backups/$backup_name"
        compose run --rm --no-deps -v "$PWD:/setup:ro" worker \
            python /setup/scripts/verify_release_backup.py "/app/backups/$backup_name"
        echo "Verified encrypted backup: $backup_name in GA_BACKUP_DIR. Worker remains stopped."
        ;;
    unpack-backup)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        backup_name=${2:-}
        if [[ ! "$backup_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*\.enc$ ]]; then
            echo "Pass the encrypted backup filename from GA_BACKUP_DIR." >&2
            exit 2
        fi
        recovery_name="${backup_name%.enc}-recovery-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        compose run --rm --no-deps worker garmin-ai unpack-backup \
            "/app/backups/$backup_name" "/app/backups/$recovery_name"
        echo "Recovery files are in GA_BACKUP_DIR/$recovery_name. Keep them private."
        ;;
    *)
        echo "Usage: ./install.sh demo | setup [configure options] | start | status | stop | backup | unpack-backup FILENAME" >&2
        exit 2
        ;;
esac
