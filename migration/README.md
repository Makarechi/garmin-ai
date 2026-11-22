# Rust migration baseline (R00)

[baseline.json](baseline.json) pins the Python version of the product that Rust must match.
[baseline-test-nodeids.txt](baseline-test-nodeids.txt) records every collected pytest case,
including parameterized cases and cases later skipped. The fixture passwords in node IDs that
embed example database URLs are replaced with `<redacted>`. Collection is evidence that a test exists.
The separate run records show which cases actually ran.

The handoff pinned `d22be2a8`. PR #204 merged before R00 started, so this baseline includes its
Gemini failure diagnostics and worker handling, along with the tests added by that PR.
The runtime code at the recorded baseline is the reference for comparisons.

## Repeat the baseline

1. Check out the SHA in `baseline.json` in a clean worktree without local configuration, credentials,
   storage directories, provider opt-ins, or personal data. Use Python 3.15.2 and install with
   `uv sync --locked --extra full`. If that Python version is unavailable on the host, build the
   checked-in Dockerfile's `builder` stage and use its interpreter. The builder pins Python and uv.
   Install Chromium with `uv run --no-sync playwright install --with-deps chromium` on Linux.
   Run pytest as a non-root user. The installer tests exercise service ownership checks.
2. Start a fresh container from the pinned TimescaleDB image in `baseline.json`. Create a database
   whose name ends in `_test` and keep the database disposable. Supply its URL through
   `GA_TEST_DATABASE_URL` and set `GA_REQUIRE_TEST_DB=1`. Keep the test and database containers on a
   private network without external provider access after dependencies are installed.
3. Run the following from the root of the pinned checkout. The database fixture creates the
   schema. Fetch the schema revision and engine versions from that same test database afterward
   with `SELECT version_num FROM alembic_version`, `SHOW server_version`, and
   `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'`.

   ```sh
   mkdir -p test-results
   uv run --no-sync python -VV
   uv run --no-sync alembic heads
   uv run --no-sync pytest --collect-only -q > test-results/collection.txt
   rg '^tests/[^:]+::' test-results/collection.txt |
     sed -E 's#(postgresql(\+psycopg)?://[^:/[:space:]]+:)[^@/[:space:]]+(@)#\1<redacted>\3#g' \
     > test-results/baseline-test-nodeids.txt
   uv run --no-sync python scripts/release_metadata.py > test-results/release-metadata.json
   uv run --no-sync pytest -q -ra --junitxml=test-results/pytest.xml
   ```

4. Record results and actual skip reasons. Run the separately installed synthetic extensions as
   described in the `CI` workflow and keep that result distinct from the default suite. The
   native amd64 installation and off-host restore workflows provide separate evidence. Keep raw
   reports out of Git and record only reviewed summaries and synthetic test identifiers.
   Reproducing the application image means building from the pinned source and Dockerfile.
   Registry and local image digests identify the resulting builds for their respective platforms.

## Track subsequent changes in main

Fetch `main` at the start of each migration PR and check
`git log --first-parent --oneline <last-accounted-main-sha>..origin/main`. Record the reviewed SHA
and the Python changes relevant to the migration in that PR's evidence. For each behavior,
schema, setting, or operational contract change, update the corresponding parity entry and carry
the new or changed Python tests into the comparison plan. A Python fix that lands after the
baseline stays visible as a delta even if the old test was already ported.

Keep this snapshot immutable. If an intentional rebaseline is needed, retain the previous SHA,
record why each accepted contract changed, and regenerate the collection and evidence for the
new SHA. Use pinned revisions for comparisons rather than comparing to whichever commit is
currently at the tip of `main`.

## Check parity evidence

Run the execution checker from the migration checkout with the JUnit report and metadata from
one frozen Python reference run.

```sh
python3 migration/check_baseline_execution.py pytest.xml release-metadata.json
```

It matches the complete frozen case inventory, checks every case cited by UT-01 through UT-53,
and reports the suite's skipped tests by module. Keep the JUnit file private and retain the JSON
summary and its report checksum. The [Linux ARM64 reference result](evidence/python-frozen-universal-linux-arm64.json)
records 4,342 passing tests and 27 skips. Every referenced universal test case passed. Native
execution still needs its own evidence.

Run `python3 migration/check_parity.py` to validate ownership of the baseline inventory and the
references to collected Python tests and native comparison cases. An `implemented` area has
native coverage at the recorded level. Its remaining work is listed in `known_limits`.
Use `verified` after the full area has been exercised.

Run `python3 migration/check_parity.py --require-verified` when checking release readiness.
The command exits unsuccessfully while any area remains unverified and lists each such area.
Execute the recorded comparisons and the required release workloads before recording a result.

## Rehearse recovery before and after native writes

The [cutover](cutover-runbook.md) and [rollback](rollback-runbook.md) procedures
connect these fixture checks to the release installation and change record.
The [lock order](lock-order.md) describes the ownership and recovery fences.

Run `python migration/check_cutover_restore.py /path/to/garmin-ai-rust` in the pinned Python
test environment. Provide three distinct empty databases on the same isolated TimescaleDB
server through `GA_TEST_DATABASE_URL`, `GA_TEST_BEFORE_RESTORE_URL` and
`GA_TEST_AFTER_RESTORE_URL`. Each database name must end in `_test`. Use an internal network
and the release binary under test.

The fixture creates an encrypted baseline copy, probes native reads, and restores the
initial copy for the Python reader. It then creates and corrects a diary record with Rust,
stops that API, and makes a second encrypted copy. Recovery places the raw files and tokens
in the restored installation and compares every table, including pending work, polling and
replay cursors, bindings and the uncertain delivery receipt. A retry on the restored native
installation returns the same corrected record. The restored Python API also returns that
record on retry, rejects a stale edit, and records a third revision that Rust reads back.
The audit contains the original create and both corrections in order. The initial copy
predates that record.

The fixture reports archive authentication, database restore and file placement times.
It also measures through a verified Python read on the earlier backup and through a verified
Python write on the later state. The latter includes the new backup. Measure operational
recovery on the final image with the independent recovery environment and queue drain.

The native off-host workflow creates and corrects a record through the Rust API before
exporting a backup. The recovery job authenticates it on a different Linux host and checks
the contents of every restored table. The workflow uses the pinned Python checkout and
locked dependencies for Python backup and recovery. On return to Python the API retries
the original request, preserves the pending work and delivery state, and writes the third
revision of the same record.
The pinned Python worker then takes the recovered job, replays its archived input once and
publishes the expected daily total. The fixture checks the released lease, unchanged delivery
receipts, and time through job completion. External integrations stay disabled in the fixture.

Another recovery job restores the same post-write backup to Rust on a different
Linux host. It verifies the restored edit, retries the original request without
duplicating it, rejects a stale correction, and saves the next revision. The
result records all three revisions after checking their audit history. The
recovered worker also completes the pending replay once and releases its lease.

For a Python backup restored to Rust, the native API must reach readiness and read
the retained diary event. The native worker must take the restored replay job once,
publish its daily total and release the runtime lease on exit. The fixture checks
that the API stays ready, the event is unchanged, and the pending clarification,
delivery receipts, and account bindings are preserved. The result records the
time from archive unpack to API readiness and the time from worker startup to
replay completion.

A separate recovery job restores the pre-switch Python backup on another Linux host.
The native read-only API sees the retained event, rejects a write, and leaves the
recovered tables unchanged. The pinned Python release then accepts and deduplicates
a new diary request and replays a job from the restored queue. The result records the
native inspector's binary digest and the ordinary delivery and binding checks.

## Rehearse worker ownership

Run `python migration/check_worker_cutover.py /path/to/garmin-ai /path/to/garmin-ai-rust`
in the same pinned test environment. Supply a fresh isolated `_test` database through
`GA_TEST_DATABASE_URL`. Both workers use the same database and storage with distinct local
lock directories. No external integrations are configured.

The test starts the pinned Python worker and checks that Rust cannot take its database
lease. It kills the Python process, starts Rust, verifies that Rust owns the lease recorded
in its heartbeat, and checks that a competing Python worker exits. After Rust stops
cleanly, the Python worker can resume. The test compares the saved pending replay job,
clarification, polling and replay cursors, account bindings and uncertain delivery receipt
throughout the handover.

Add `--replay-backlog` and use another fresh test database to exercise two real offline
jobs from a synthetic archive. The test kills Python after it claims a job while an
archive write is blocked. Rust defers the other job during storage contention, releases
its attempt, and completes it after the lock becomes available. It then waits for the
original Python claim to expire before completing that job. Allow just over five minutes
for the unmodified Python lease. Both projections and source identities are checked
after returning worker ownership to Python.

The release rehearsal also needs the final package, a representative queue and external
provider delivery checks.
