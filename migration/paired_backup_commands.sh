#!/bin/sh
# Run the normal release commands in the measured runtime, with synthetic-only mounts.
set -eu
cli=$1
root=/backup-fixture
low=/tmp/low-backup-space/insufficient.enc
phase=capacity
trap 'result=$?; if [ "$result" -ne 0 ]; then printf "Paired backup phase %s failed\n" "$phase" >&2; fi' EXIT

dd if=/dev/zero of=/tmp/low-backup-space/synthetic-filler bs=1M count=6 status=none
"$cli" backup-space "$low" > "$root/space.json"
if "$cli" backup "$low" > /tmp/backup-low-stdout 2> /tmp/backup-low-stderr; then
    echo "Backup unexpectedly succeeded on the constrained mount" >&2
    exit 1
fi
test ! -e "$low"
rm /tmp/low-backup-space/synthetic-filler

phase=backup
start=$(date +%s%N)
"$cli" backup "$root/complete.enc" > "$root/backup-counts.json"
after_backup=$(date +%s%N)
phase=export
"$cli" export "$root/export.jsonl.gz" > "$root/export-counts.json"
after_export=$(date +%s%N)
phase=authentication
"$cli" unpack-backup "$root/complete.enc" "$root/unpacked" >/dev/null
phase=restore
GA_DATABASE_URL="$GA_RESTORE_DATABASE_URL" GA_DATA_DIR="$root/recovery-data" \
    GA_LOCK_DIR="$root/recovery-locks" "$cli" restore-db "$root/unpacked/database.jsonl.gz" \
    > "$root/restore-counts.json"
finished=$(date +%s%N)
printf '{"backup_ns":%s,"export_ns":%s,"authenticate_unpack_restore_ns":%s}\n' \
    "$((after_backup - start))" "$((after_export - after_backup))" "$((finished - after_export))"
