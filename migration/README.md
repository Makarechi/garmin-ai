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
