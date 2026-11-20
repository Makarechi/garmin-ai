#!/usr/bin/env bash
# Extract the executable shipped by the actual native release container.
set -euo pipefail

if [[ $# != 1 || -e $1 ]]; then
    echo "Specify a new path for the packaged test executable." >&2
    exit 2
fi
destination=$1
root=$(cd "$(dirname "$0")/.." && pwd)
revision=$(git -C "$root" rev-parse HEAD)
image="garmin-ai-native-proof:$revision"
docker build -f "$root/Dockerfile.rust" --label "org.opencontainers.image.revision=$revision" \
    -t "$image" "$root"
docker run --rm --network none --entrypoint sh "$image" -ec \
    'test "$(id -u)" -ne 0 && ! command -v python && ! command -v python3'
container=$(docker create "$image")
trap 'docker rm "$container" >/dev/null' EXIT
docker cp "$container:/usr/local/bin/garmin-ai-rust" "$destination"
chmod 0555 "$destination"
"$destination" --version
