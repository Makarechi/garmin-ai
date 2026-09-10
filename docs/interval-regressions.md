# GA-28: generated interval regressions

`tests/test_interval_invariants.py` links the supplied R01 (old open episodes) and
R02 (point/boundary/DST/status/deletion) cases to generated PostgreSQL checks.
Run `pytest -m regression tests/test_interval_invariants.py` with the required isolated
test database. The marker records the source case ID; it does not imply all 80 supplied
cases have been mapped or all 32 tasks completed.

A fixed seed generates interior/exterior timestamps alongside exact query boundaries
and partition cuts. Caffeine, migraine and illness combine point, zero-length, bounded
and open topology with all supported statuses and audited deletion. Six civil days
cover spring/fall transitions in Budapest, New York and Lord Howe (including half-hour
DST). These are real SQL queries through both the query function and shared tool path.

The assertions use invariants rather than copying the SQL predicate: adjacent queries
must union to the whole query; equivalent instants in four timezone representations
must return the same identities; deleted rows stay absent; past open episodes stay
present; reads cannot mutate event timestamps/revisions or the audit count. Existing
specific boundary repros remain in `test_event_topology.py`.

This is deterministic generated coverage, not exhaustive fuzzing or a seven-day chaos
run. Other regression IDs, provider-independent conversation evals, coverage reporting,
upstream compatibility expansion and unattended acceptance remain later GA-28 work.
