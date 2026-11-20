#!/usr/bin/env bash
set -euo pipefail

preview=false
if [[ "${1:-}" == --preview ]]; then
    preview=true
    shift
fi
if [[ "$(id -u)" != 0 || "$#" != 2 ]]; then
    echo "Usage: sudo deploy/gcp/install-native-cleanup.sh [--preview] VERIFIED_ROLLBACK_IMAGE UPLOAD_DIRECTORY" >&2
    exit 2
fi
uploads=$2
if [[ "$uploads" != /* || "$uploads" == / || "$uploads" == *[$'\n\r%"\\']* ||
      ! -d "$uploads" || "$(realpath -e -- "$uploads")" != "$uploads" ]]; then
    echo "Upload directory must be an existing canonical absolute path." >&2
    exit 2
fi

base=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
release=$base/../../release.env
image=$(sed -n 's/^GA_APP_IMAGE=//p' "$release")
runtime=$(sed -n 's/^GA_APP_RUNTIME=//p' "$release")
if [[ "$runtime" != rust || ! "$image" =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]]; then
    echo "This installer requires the digest-pinned image of a native release bundle." >&2
    exit 2
fi

# The cleanup state, upload directory, and Docker daemon must be on the same host.
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
tool_dir=/usr/local/lib/garmin-ai
state=/var/lib/garmin-ai-release-cleanup/state.json
unit_dir=/etc/systemd/system
service=garmin-ai-release-cleanup.service
timer=garmin-ai-release-cleanup.timer
install -d -m 0755 "$tool_dir"
stage=$(mktemp -d "$tool_dir/.cleanup.XXXXXX")
container=
cleanup() {
    if [[ -n "$container" ]]; then
        docker rm "$container" >/dev/null || true
    fi
    rm -rf -- "$stage"
}
trap cleanup EXIT

container=$(docker create --network none --entrypoint /usr/local/bin/garmin-ai-release-cleanup "$image" --help)
docker cp "$container:/usr/local/bin/garmin-ai-release-cleanup" "$stage/garmin-ai-release-cleanup"
docker rm "$container" >/dev/null
container=
chmod 0555 "$stage/garmin-ai-release-cleanup"

args=(--archive-dir "$uploads" --preserve-archive-dirs)
if [[ ! -e "$state" ]]; then
    args+=(--initialize-rollback "$1")
fi
if [[ "$preview" == true ]]; then
    "$stage/garmin-ai-release-cleanup" "${args[@]}"
    exit
fi

report=$("$stage/garmin-ai-release-cleanup" "${args[@]}" --apply --record-only) || {
    printf '%s\n' "$report" >&2
    exit 1
}
printf '%s\n' "$report"
if [[ "$report" == '{"skipped":'* ]]; then
    echo "Wait for the deployment or cleanup lock before installing the timer." >&2
    exit 1
fi
mv -Tf -- "$stage/garmin-ai-release-cleanup" "$tool_dir/garmin-ai-release-cleanup"

install -d -m 0755 "$unit_dir/$service.d"
install -m 0644 "$base/garmin-ai-release-cleanup-native.service" "$unit_dir/$service"
install -m 0644 "$base/$timer" "$unit_dir/$timer"
printf '[Service]\nReadWritePaths="%s"\n' "$uploads" > "$stage/uploads.conf"
install -m 0644 "$stage/uploads.conf" "$unit_dir/$service.d/uploads.conf"
systemctl daemon-reload
systemctl enable --now "$timer"
echo "Timer enabled. Preview with: sudo $tool_dir/garmin-ai-release-cleanup"
