# CI verification evidence (GA-28, first phase)

CI sets `GA_REQUIRE_TEST_DB=1`. An absent test URL fails the session before tests
can silently skip database coverage. The URL must identify PostgreSQL and a database
whose name ends in `_test`; malformed URLs, missing names and other backends are
rejected before constructing an engine. Validation errors do not include credentials.
An unavailable database fails connection/migration rather than becoming a skip.

Local test runs may still omit the database URL: database fixtures explicitly skip,
which is not evidence of database validation. Use `-ra` to display skip reasons.

CI writes a JUnit XML report and uploads it even when tests fail. The artifact records
individual outcomes and skip reasons; opt-in provider tests remain separate from the
deterministic database suite. No original health fixture is introduced by this phase.

The name suffix is an operator guard, not proof a database is disposable. Always supply
a separately provisioned synthetic database. Tests truncate that database.

Coverage measurement, regression-ID mapping, property-based/chaos suites, dependency
contract gates and the seven-day unattended run remain subsequent GA-28 phases.
