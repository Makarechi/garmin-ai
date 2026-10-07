#!/bin/sh
# Run a digest-pinned, source-free installation bundle with Docker only.
set -eu

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Docker with the Compose plugin is required." >&2
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
            -p 127.0.0.1:8765:8765 \
            --mount "type=bind,source=$PWD,target=/setup,readonly" \
            --entrypoint python "$image" \
            /setup/scripts/serve_demo.py --host 0.0.0.0 --port 8765 "$@"
        ;;
    setup)
        shift
        docker run --rm \
            --user "$(id -u):$(id -g)" \
            --mount "type=bind,source=$PWD,target=$PWD" \
            --workdir "$PWD" --entrypoint python "$image" \
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
    *)
        echo "Usage: ./install.sh demo | setup [configure options] | start | status | stop" >&2
        exit 2
        ;;
esac
