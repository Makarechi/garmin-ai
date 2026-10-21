#!/bin/sh
set -eu
if [ "$(id -u)" != 0 ] || [ "$#" -ne 2 ]; then
    echo "Usage: sudo deploy/gcp/install-cleanup.sh VERIFIED_ROLLBACK_IMAGE UPLOAD_DIRECTORY" >&2
    exit 1
fi
base=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /usr/local/lib/garmin-ai
install -d -m 0700 /var/lib/garmin-ai-release-cleanup
install -m 0644 "$base/release_cleanup.py" /usr/local/lib/garmin-ai/release_cleanup.py
install -m 0644 "$base/garmin-ai-release-cleanup.service" /etc/systemd/system/
install -m 0644 "$base/garmin-ai-release-cleanup.timer" /etc/systemd/system/
# Permit unlinking only inside the selected upload directory in the unit sandbox.
install -d -m 0755 /etc/systemd/system/garmin-ai-release-cleanup.service.d
python3 - "$1" "$2" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

directory = Path(sys.argv[2])
if not directory.is_dir() or directory.resolve() != directory or any(c in str(directory) for c in '\n\r%"\\'):
    raise SystemExit("Upload directory must be an existing canonical absolute path")
state = Path('/var/lib/garmin-ai-release-cleanup/state.json')
args = ['python3', '/usr/local/lib/garmin-ai/release_cleanup.py', '--apply', '--record-only']
if not state.exists():
    args += ['--initialize-rollback', sys.argv[1], '--archive-dir', str(directory)]
elif json.loads(state.read_text())['archive_dirs'] != [str(directory)]:
    raise SystemExit('Existing archive directories differ; review state before changing them')
subprocess.run(args, check=True)
Path('/etc/systemd/system/garmin-ai-release-cleanup.service.d/uploads.conf').write_text(
    '[Service]\nReadWritePaths="' + str(directory) + '"\n'
)
PY
systemctl daemon-reload
systemctl enable --now garmin-ai-release-cleanup.timer
echo "Timer enabled. Preview with: sudo python3 /usr/local/lib/garmin-ai/release_cleanup.py"
