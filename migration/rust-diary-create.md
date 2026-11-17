# Native diary creation

Built-in event creation verifies the configured local owner and collection or
tracking permissions. It binds the same active system definition as Python and
validates references to activities and migraine episodes. The event, creation
audit and applicable metric observations commit together.

A retry validates the original creation audit and returns the current revision.
It does not consume another audit or metric projection. Historical caffeine
defaults and equivalent timestamp offsets match the original request. Unknown
medication values remain null. Published metric facts use the event's local
calendar date and the original observation, recording and ingestion clocks.

The fixture began with a Python-created diary entry and metric binding. Rust
reused that entry, created one of its own, and rejected a changed retry. The
Python implementation then replayed the Rust creation audit and found its
newer revision. The same cases passed on the development host and native Linux
arm64 against disposable PostgreSQL 17.11 and TimescaleDB 2.29.2. A forced
metric insert failure rolled back the event and audit as well.

Run the compatibility check against an explicitly empty disposable database
through the environment used by the existing migration tests.

```sh
cargo test --locked -p ga-db --test diary --no-run --message-format=json
uv run python migration/check_diary_creation.py /path/to/the/diary-test-binary
```

Corrections, delete and undo use separate lifecycle steps. The native web and
channel handlers will call the same diary store.

## Correcting a stored event

Corrections require the expected revision. The write retains the original
creation audit and appends a before-and-after audit for the new revision. It
preserves related entries, rejects a change that would put a symptom outside
its migraine episode, and updates the associated question when the episode
closes or reopens. The prior metric observations receive an invalidation time.
New observations use the next generation.

The native Linux arm64 test covered a stale revision, two generations of a
caffeine metric, audit snapshots, changes to a linked migraine episode, and
reopening a sent question after expiry. Python replay still found the event
through its original creation audit.
