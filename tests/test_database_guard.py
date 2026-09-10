import conftest
import pytest


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://user:synthetic-secret@localhost/garmin_ai",
        "postgresql+psycopg://user:synthetic-secret@localhost/",
        "sqlite:///garmin_ai_test",
        "postgresql+psycopg://user:synthetic-secret@localhost/dummy_test?dbname=garmin_ai",
        "postgresql+psycopg://user:synthetic-secret@localhost/dummy_test?service=production",
        "postgresql+psycopg://user:synthetic-secret@localhost/dummy_test?dbname=a_test&dbname=garmin_ai",
        "not a database URL synthetic-secret",
    ],
)
def test_invalid_database_is_rejected_before_engine_creation(monkeypatch, url):
    monkeypatch.setenv("GA_TEST_DATABASE_URL", url)
    touched = []
    monkeypatch.setattr(conftest, "create_engine", lambda *args, **kwargs: touched.append(args))
    with pytest.raises(pytest.UsageError) as error:
        next(conftest.db_engine.__wrapped__())
    assert not touched
    assert "synthetic-secret" not in str(error.value)


def test_required_database_cannot_silently_skip(monkeypatch):
    monkeypatch.delenv("GA_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("GA_REQUIRE_TEST_DB", "1")
    with pytest.raises(pytest.UsageError, match="required"):
        conftest.pytest_configure(None)


def test_optional_local_database_remains_explicit_skip(monkeypatch):
    monkeypatch.delenv("GA_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("GA_REQUIRE_TEST_DB", raising=False)
    with pytest.raises(pytest.skip.Exception, match="dedicated PostgreSQL"):
        next(conftest.db_engine.__wrapped__())


def test_dedicated_postgres_url_is_accepted_without_connecting(monkeypatch):
    url = "postgresql+psycopg://user:synthetic-secret@localhost/garmin_ai_test"
    monkeypatch.setenv("GA_TEST_DATABASE_URL", url)
    assert conftest.test_database_url() == url
