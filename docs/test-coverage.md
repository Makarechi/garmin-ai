# GA-28: inspectable test coverage

CI runs the existing PostgreSQL test suite with branch coverage enabled and uploads
Cobertura XML and browsable HTML alongside JUnit in the `pytest-results` artifact,
even when tests fail. The terminal summary also lists missing lines. Development
dependencies are resolved in `uv.lock`; production dependencies are unchanged.

Reproduce against a disposable test database with the project's test environment:

```sh
uv sync --locked
uv run pytest -q -ra --junitxml=test-results/pytest.xml --cov=garmin_ai --cov-branch --cov-report=term --cov-report=xml:test-results/coverage.xml --cov-report=html:test-results/htmlcov
```

Reports use repository-relative source paths. `test-results/` is ignored by Git.
The coverage command measures Python execution in the test process; subprocess,
frontend JavaScript, real provider and real-device behavior are not inferred from
that percentage. Opt-in and platform skips remain visible in JUnit and `-ra`.
No percentage threshold is imposed before a reviewed baseline exists. Passing
coverage generation does not certify behavior or replace regression assertions.

Report options follow the [pytest-cov reporting documentation](https://pytest-cov.readthedocs.io/en/stable/reporting.html).
The remaining GA-28 work includes regression-ID mapping, conversation evals,
chaos scenarios and a measured seven-day unattended run.
