#!/usr/bin/env bash
# Run a digest-pinned, source-free installation bundle with Docker only.
set -euo pipefail

cd "$(dirname "$0")"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this installer as a dedicated non-root user with Docker access." >&2
    exit 1
fi

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
runtime=$(sed -n 's/^GA_APP_RUNTIME=//p' release.env)
legacy_entry=
case "${runtime:-python}" in
    rust) ;;
    python) legacy_entry=garmin-ai ;;
    *) echo "release.env must select the rust or python application runtime." >&2; exit 1 ;;
esac

install_command=./install.sh
compose_files=(-f compose.release.yml)
if [[ "${1:-}" == --gcp ]]; then
    if [[ "$runtime" != rust || ! -f compose.gcp.yml ]]; then
        echo "The GCP profile requires a native bundle containing compose.gcp.yml." >&2
        exit 2
    fi
    shift
    install_command+=' --gcp'
    compose_files+=(-f compose.gcp.yml)
fi

compose() (
    # Shell exports override --env-file interpolation. Keep Docker connection
    # settings, but remove every bundle and Compose override in this subshell.
    while IFS= read -r name; do
        case "$name" in
            GA_*|COMPOSE_*) unset "$name" ;;
        esac
    done < <(compgen -e)
    GA_APP_IMAGE="$image" docker compose --env-file .env --env-file release.env "${compose_files[@]}" "$@"
)

require_stopped_runtime() {
    local active
    active=$(compose ps --status running --status restarting --status paused --quiet api worker) || return
    if [[ -n "$active" ]]; then
        echo "$1" >&2
        return 1
    fi
}

stop_inspection() {
    if docker container inspect "$inspection_name" >/dev/null 2>&1 &&
        ! docker container stop "$inspection_name" >/dev/null 2>&1 &&
        docker container inspect "$inspection_name" >/dev/null 2>&1; then
        echo "Could not stop the read-only inspection container." >&2
        return 1
    fi
}

case "${1:-}" in
    demo)
        shift
        if [ -z "$legacy_entry" ]; then
            exec docker run --rm --read-only --cap-drop ALL \
                --user "$(id -u):$(id -g)" -p 127.0.0.1:8765:8765 \
                "$image" demo --host 0.0.0.0 --port 8765 "$@"
        fi
        exec docker run --rm --read-only \
            --user "$(id -u):$(id -g)" \
            -p 127.0.0.1:8765:8765 \
            --mount "type=bind,source=$PWD,target=/setup,readonly" \
            --entrypoint python "$image" \
            /setup/scripts/serve_demo.py --host 0.0.0.0 --port 8765 "$@"
        ;;
    licenses)
        if [ -z "$legacy_entry" ]; then
            exec docker run --rm --read-only --entrypoint cat "$image" \
                /usr/share/doc/garmin-ai/LICENSE \
                /usr/share/doc/garmin-ai/LICENSE-scipy.txt \
                /usr/share/doc/garmin-ai/LICENSE-python.txt \
                /usr/share/doc/garmin-ai/LICENSE-fitdecode.txt \
                /usr/share/doc/garmin-ai/rust-third-party-notices.txt
        fi
        docker run --rm --read-only --entrypoint cat "$image" \
            /usr/share/doc/garmin-ai/LICENSE \
            /usr/share/doc/garmin-ai/python-third-party-notices.txt
        ;;
    setup)
        shift
        if [ -z "$legacy_entry" ]; then
            mount_list=$(docker run --rm --read-only --cap-drop ALL --network none \
                --user "$(id -u):$(id -g)" --workdir /setup \
                --mount "type=bind,source=$PWD,target=/setup,readonly" \
                "$image" configure --mount-sources "$PWD")
        else
            mount_list=$(docker run --rm --read-only \
                --user "$(id -u):$(id -g)" \
                --mount "type=bind,source=$PWD,target=/setup,readonly" \
                --entrypoint python "$image" \
                /setup/scripts/release_mounts.py /setup/.env "$PWD")
        fi
        mount_args=(--mount "type=bind,source=$PWD,target=$PWD")
        while IFS= read -r directory; do
            if [ -n "$directory" ]; then
                mount_args+=(--mount "type=bind,source=$directory,target=$directory")
            fi
        done <<< "$mount_list"
        if [ -z "$legacy_entry" ]; then
            docker run --rm --cap-drop ALL --network none \
                --user "$(id -u):$(id -g)" "${mount_args[@]}" --workdir "$PWD" \
                "$image" configure "$@"
        else
            docker run --rm --user "$(id -u):$(id -g)" "${mount_args[@]}" \
                --workdir "$PWD" --entrypoint python "$image" "$PWD/scripts/configure.py" "$@"
        fi
        compose config --quiet
        ;;
    start)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose config --quiet
        compose up -d --wait
        ;;
    migrate|inspect)
        test "$runtime" = rust || { echo "This command requires a native bundle." >&2; exit 2; }
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        if [[ "$1" == migrate && $# -eq 2 && "$2" == --status ]]; then
            compose run --rm --no-deps migrate migrate --status
            exit
        fi
        test "$#" -eq 1 || { echo "Unexpected command arguments." >&2; exit 2; }
        require_stopped_runtime "Stop the API and worker before preparing a cutover."
        if [[ "$1" == migrate ]]; then
            compose up -d --wait db
            compose run --rm --no-deps migrate
        else
            inspection_name="garmin-ai-inspect-$$-$RANDOM-$RANDOM"
            if docker container inspect "$inspection_name" >/dev/null 2>&1; then
                echo "The inspection container name is already in use." >&2
                exit 1
            fi
            trap stop_inspection EXIT
            trap 'exit 130' INT
            trap 'exit 143' TERM
            compose run --rm --no-deps --name "$inspection_name" --service-ports api \
                serve --read-only --host 0.0.0.0 --port 8080
        fi
        ;;
    status)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose ps
        ;;
    stop)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose stop
        ;;
    login)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose stop worker
        compose run --rm --no-deps worker ${legacy_entry:+"$legacy_entry"} login "${@:2}"
        echo "Garmin login finished. Run $install_command start to recreate the worker."
        ;;
    pair-telegram)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose stop worker
        compose run --rm --no-deps --workdir "$PWD" \
            -v "$PWD:$PWD" worker ${legacy_entry:+"$legacy_entry"} pair-telegram --env-file "$PWD/.env" --container-runtime
        echo "Pairing finished. Run $install_command start to recreate API and worker with the saved owner ID."
        ;;
    backup-space)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        test "$#" -eq 1 || { echo "Unexpected command arguments." >&2; exit 2; }
        compose run --rm --no-deps -T worker ${legacy_entry:+"$legacy_entry"} backup-space /app/backups/backup.enc
        ;;
    export)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        if [[ $# -ne 2 || ! "${2:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*\.jsonl\.gz$ ]]; then
            echo "Choose a new .jsonl.gz filename for the export in GA_DATA_DIR." >&2
            exit 2
        fi
        compose run --rm --no-deps -T worker ${legacy_entry:+"$legacy_entry"} export "/app/data/$2"
        ;;
    backup)
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        compose config --quiet
        worker_identity=$(compose run --rm --no-deps -T --entrypoint sh worker \
            -ec 'printf "%s:%s\n" "$(id -u)" "$(id -g)"')
        if [[ ! "$worker_identity" =~ ^[1-9][0-9]*:[0-9]+$ ]]; then
            echo "Could not determine the non-root backup worker identity." >&2
            exit 1
        fi
        compose stop worker
        backup_name="manual-$(date -u +%Y%m%dT%H%M%SZ)-$$.enc"
        compose run --rm --no-deps worker ${legacy_entry:+"$legacy_entry"} backup "/app/backups/$backup_name"
        verify_volume=$(docker volume create)
        trap 'docker volume rm -f "$verify_volume" >/dev/null' EXIT
        docker run --rm --network none --user 0 -v "$verify_volume:/verification" --entrypoint chown \
            "$image" "$worker_identity" /verification
        if [ -z "$legacy_entry" ]; then
            compose run --rm --no-deps -v "$verify_volume:/verification" worker \
                unpack-backup "/app/backups/$backup_name" /verification/extracted
        else
            compose run --rm --no-deps -v "$PWD:/setup:ro" -v "$verify_volume:/verification" worker \
                python /setup/scripts/verify_release_backup.py "/app/backups/$backup_name"
        fi
        docker volume rm "$verify_volume" >/dev/null
        trap - EXIT
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
        compose run --rm --no-deps worker ${legacy_entry:+"$legacy_entry"} unpack-backup \
            "/app/backups/$backup_name" "/app/backups/$recovery_name"
        echo "Recovery files are in GA_BACKUP_DIR/$recovery_name. Keep them private."
        ;;
    restore-db)
        test "$runtime" = rust || { echo "This command requires a native bundle." >&2; exit 2; }
        test -f .env || { echo "Run ./install.sh setup first." >&2; exit 1; }
        if [[ $# -ne 2 || ! "${2:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
            echo "Pass the recovery directory name from GA_BACKUP_DIR." >&2
            exit 2
        fi
        require_stopped_runtime "Stop the API and worker before restoring the database."
        compose run --rm --no-deps -T worker restore-db "/app/backups/$2/database.jsonl.gz"
        ;;
    *)
        echo "Usage: ./install.sh [--gcp] {demo | licenses | setup [configure options] | start | status | stop | login [--confirm-existing-owner] | pair-telegram | backup-space | export FILENAME.jsonl.gz | backup | unpack-backup FILENAME | restore-db RECOVERY_DIRECTORY | migrate [--status] | inspect}" >&2
        exit 2
        ;;
esac
