import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, select

from garmin_ai.pairing import discover_owner, load_pairing, save_owner

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def message(identity=42, **changes):
    values = dict(
        chat=NS(type="private", id=identity),
        from_user=NS(id=identity, is_bot=False),
        date=NOW,
        forward_origin=None,
        text="/pair synthetic-code",
    )
    values.update(changes)
    return NS(**values)


def test_pairing_ignores_wrong_forwarded_and_group_messages():
    items = [
        message(text="/pair wrong"),
        message(forward_origin=NS()),
        message(chat=NS(type="group", id=42)),
        message(from_user=NS(id=7, is_bot=False)),
        message(),
    ]

    class Bot:
        async def get_updates(self, **kwargs):
            return [NS(update_id=i, message=value) for i, value in enumerate(items)]

    assert asyncio.run(discover_owner(Bot(), "synthetic-code", NOW)) == 42


def test_pairing_timeout_never_claims_an_owner():
    ticks = iter([0, 0, 0, 181])

    class Bot:
        async def get_updates(self, **kwargs):
            return []

    with pytest.raises(TimeoutError):
        asyncio.run(discover_owner(Bot(), "synthetic-code", NOW, clock=lambda: next(ticks)))


@pytest.mark.parametrize("existing", ["", "GA_TELEGRAM_USER_ID=0\n"])
def test_pairing_preserves_secrets_and_cannot_replace_owner(tmp_path, existing):
    path = tmp_path / ".env"
    content = (
        "# preserved\nGA_TELEGRAM_BOT_TOKEN='synthetic-secret'\nGA_API_KEY='synthetic-other-secret'\n"
        + existing
    )
    path.write_text(content)
    original, _ = load_pairing(path)
    save_owner(path, original, 42)
    assert "GA_TELEGRAM_USER_ID='42'" in path.read_text()
    assert "GA_API_KEY='synthetic-other-secret'" in path.read_text()
    with pytest.raises(ValueError, match="already configured"):
        load_pairing(path)
    with pytest.raises(ValueError):
        save_owner(path, original, 43)


def test_changed_environment_is_not_overwritten(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GA_TELEGRAM_BOT_TOKEN='synthetic'\n")
    original, _ = load_pairing(path)
    path.write_bytes(original + b"# changed\n")
    with pytest.raises(ValueError, match="changed"):
        save_owner(path, original, 42)
    assert path.read_bytes() == original + b"# changed\n"


def test_pairing_rejects_explicit_allowlist_without_primary_telegram(tmp_path):
    path = tmp_path / ".env"
    original = (
        "GA_TELEGRAM_BOT_TOKEN='synthetic'\n"
        'GA_INTEGRATIONS=\'[{"id":"source:garmin:primary",'
        '"kind":"source","provider":"garmin"}]\'\n'
    )
    path.write_text(original)

    with pytest.raises(ValueError, match="channel:telegram:primary"):
        load_pairing(path)

    assert path.read_text() == original


def test_alternate_env_file_uses_its_own_default_lock_directory(tmp_path, monkeypatch):
    path = tmp_path / "instance" / ".env"
    path.parent.mkdir()
    path.write_text("GA_TELEGRAM_BOT_TOKEN='synthetic'\n")
    monkeypatch.setenv("GA_LOCK_DIR", "/unrelated")
    _, config = load_pairing(path)
    assert config.lock_dir == path.parent / ".state"


def test_pairing_rejects_nonpositive_recovered_owner(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GA_TELEGRAM_BOT_TOKEN='synthetic'\nGA_TELEGRAM_USER_ID='-1'\n")
    with pytest.raises(ValueError, match="positive ID"):
        load_pairing(path, allow_configured=True)


def test_multiline_environment_values_survive_pairing(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "GA_TELEGRAM_BOT_TOKEN='synthetic'\nOTHER='line one\nline two'\nGA_TELEGRAM_USER_ID='0'\n"
    )
    original, _ = load_pairing(path)
    save_owner(path, original, 42)
    assert "OTHER='line one\nline two'" in path.read_text()


def test_match_after_deadline_does_not_pair():
    ticks = iter([0, 0, 0, 181])

    class Bot:
        async def get_updates(self, **kwargs):
            return [NS(update_id=1, message=message())]

    with pytest.raises(TimeoutError):
        asyncio.run(discover_owner(Bot(), "synthetic-code", NOW, clock=lambda: next(ticks)))


def test_cli_routes_pairing_to_explicit_environment_file(monkeypatch, tmp_path):
    from garmin_ai import cli, pairing

    called = []

    async def fake(path):
        called.append(path)

    monkeypatch.setattr(pairing, "pair_telegram", fake)
    monkeypatch.setattr(
        cli, "Settings", lambda: pytest.fail("Ambient settings must not select another instance")
    )
    import sys

    path = tmp_path / ".env"
    monkeypatch.setattr(sys, "argv", ["garmin-ai", "pair-telegram", "--env-file", str(path)])
    cli.main()
    assert called == [path]


@pytest.mark.parametrize("webhook", [False, True])
def test_complete_pairing_flow_uses_local_code_and_never_prints_bot_token(
    db, db_engine, tmp_path, monkeypatch, capsys, webhook
):
    from garmin_ai import pairing

    path = tmp_path / ".env"
    path.write_text(
        "GA_TELEGRAM_BOT_TOKEN='synthetic-private-token'\n"
        f"GA_DATABASE_URL='{db_engine.url.render_as_string(hide_password=False)}'\n"
    )
    original = path.read_bytes()

    class Bot:
        def __init__(self, token):
            assert token == "synthetic-private-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_webhook_info(self):
            return NS(url="https://synthetic.invalid" if webhook else "")

        async def get_me(self):
            return NS(username="synthetic_bot")

        async def get_updates(self, **kwargs):
            return [NS(update_id=1, message=message(date=datetime.now(UTC)))]

    monkeypatch.setattr(pairing, "Bot", Bot)
    monkeypatch.setattr(pairing.secrets, "token_urlsafe", lambda _: "synthetic-code")
    if webhook:
        with pytest.raises(ValueError, match="webhook"):
            asyncio.run(pairing.pair_telegram(path))
        assert path.read_bytes() == original
    else:
        asyncio.run(pairing.pair_telegram(path))
        assert "GA_TELEGRAM_USER_ID='42'" in path.read_text()
        from garmin_ai.models import ChannelBinding

        db.expire_all()
        binding = db.scalar(select(ChannelBinding))
        assert binding.external_id == "42"
        assert binding.confirmation_method == "local_pairing_code"
    output = capsys.readouterr().out
    assert "synthetic-private-token" not in output
    if not webhook:
        import shlex

        selected = shlex.quote(str(path.resolve()))
        assert f"GA_WORKER_ENV_FILE={selected} docker compose --env-file {selected}" in output
        assert "--force-recreate api worker" in output


def test_pairing_accepts_host_clock_skew_and_confirms_matched_update():
    calls = []

    class Bot:
        async def get_updates(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return [NS(update_id=7, message=message(date=NOW - timedelta(minutes=5)))]
            assert kwargs["offset"] == 8 and kwargs["timeout"] == 0
            return []

    assert asyncio.run(discover_owner(Bot(), "synthetic-code", NOW)) == 42
    assert len(calls) == 2


def test_pairing_interpolates_selected_file_without_rewriting_secrets(tmp_path):
    path = tmp_path / "instance.env"
    original = (
        "BOT_TOKEN=synthetic-token\nGA_TELEGRAM_BOT_TOKEN=${BOT_TOKEN}\n"
        "DB_PASSWORD=synthetic-password\n"
        "GA_DATABASE_URL=postgresql+psycopg://garmin:${DB_PASSWORD}@localhost/garmin_ai\n"
    )
    path.write_text(original)
    raw, config = load_pairing(path)
    assert config.telegram_bot_token.get_secret_value() == "synthetic-token"
    assert "synthetic-password" in config.database_url.get_secret_value()
    save_owner(path, raw, 42)
    assert path.read_text().startswith(original)
    assert "GA_TELEGRAM_USER_ID='42'" in path.read_text()


@pytest.mark.parametrize("key", ["ga_telegram_user_id", "Ga_Telegram_User_Id"])
def test_pairing_refuses_case_insensitive_existing_owner(tmp_path, key):
    path = tmp_path / ".env"
    path.write_text(f"GA_TELEGRAM_BOT_TOKEN=synthetic\n{key}=42\n")
    with pytest.raises(ValueError, match="already configured"):
        load_pairing(path)


def test_pairing_replaces_case_insensitive_empty_owner(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GA_TELEGRAM_BOT_TOKEN=synthetic\nga_telegram_user_id=0\n")
    original, _ = load_pairing(path)
    save_owner(path, original, 42)
    assert "ga_telegram_user_id" not in path.read_text()
    assert "GA_TELEGRAM_USER_ID='42'" in path.read_text()


def test_pairing_rejects_persisted_binding_before_reading_updates(
    db, db_engine, tmp_path, monkeypatch
):
    from garmin_ai import pairing
    from garmin_ai.accounts import bind_channel

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="42",
        confirmed=True,
    )
    db.commit()
    path = tmp_path / ".env"
    path.write_text(
        "GA_TELEGRAM_BOT_TOKEN='synthetic'\n"
        f"GA_DATABASE_URL='{db_engine.url.render_as_string(hide_password=False)}'\n"
    )

    class Bot:
        def __init__(self, token):
            pytest.fail("Persisted binding must be checked before Telegram is contacted")

    monkeypatch.setattr(pairing, "Bot", Bot)
    with pytest.raises(ValueError, match="already bound in the database"):
        asyncio.run(pairing.pair_telegram(path))
    assert "GA_TELEGRAM_USER_ID" not in path.read_text()


def test_pairing_requires_migration_before_contacting_telegram(db_engine, monkeypatch):
    from garmin_ai import pairing
    from garmin_ai.config import Settings

    isolated = create_engine(
        db_engine.url,
        connect_args={"options": "-c search_path=pg_catalog"},
        hide_parameters=True,
    )
    monkeypatch.setattr(pairing, "make_engine", lambda settings: isolated)

    with pytest.raises(ValueError, match="migration is required"):
        pairing.ensure_unbound_database(Settings())

    called = []
    with pytest.raises(ValueError, match="migration is required"):
        pairing.reserve_database_owner(Settings(), 42, before_commit=lambda: called.append(True))
    assert called == []


def test_pairing_database_reservation_rolls_back_when_environment_write_fails(db, db_engine):
    from sqlalchemy import func, select

    from garmin_ai import pairing
    from garmin_ai.config import Settings
    from garmin_ai.models import ChannelBinding

    def fail():
        raise OSError("synthetic write failure")

    with pytest.raises(OSError, match="synthetic write failure"):
        pairing.reserve_database_owner(
            Settings(database_url=db_engine.url.render_as_string(hide_password=False)),
            42,
            before_commit=fail,
        )
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 0


def test_pairing_reconciles_published_owner_without_contacting_telegram(
    db, db_engine, tmp_path, monkeypatch
):
    from garmin_ai import pairing
    from garmin_ai.models import ChannelBinding

    path = tmp_path / ".env"
    path.write_text(
        "GA_TELEGRAM_BOT_TOKEN='synthetic'\n"
        "GA_TELEGRAM_USER_ID='42'\n"
        f"GA_DATABASE_URL='{db_engine.url.render_as_string(hide_password=False)}'\n"
    )

    class Bot:
        def __init__(self, token):
            pytest.fail("Reconciliation must not contact Telegram")

    monkeypatch.setattr(pairing, "Bot", Bot)
    asyncio.run(pairing.pair_telegram(path))

    db.expire_all()
    assert db.scalar(select(ChannelBinding.external_id)) == "42"


@pytest.mark.parametrize("committed", [False, True])
def test_pairing_reconciles_after_uncertain_commit(db, db_engine, tmp_path, monkeypatch, committed):
    from contextlib import contextmanager

    from garmin_ai import pairing
    from garmin_ai.config import Settings
    from garmin_ai.models import ChannelBinding

    path = tmp_path / ".env"
    path.write_text("GA_TELEGRAM_BOT_TOKEN='synthetic'\nGA_TELEGRAM_USER_ID='0'\n")
    original = path.read_bytes()
    real_transaction = pairing.transaction

    @contextmanager
    def uncertain_transaction(engine):
        if committed:
            with real_transaction(engine) as session:
                yield session
            raise OSError("synthetic lost acknowledgement")
        with real_transaction(engine) as session:
            yield session
            raise OSError("synthetic rollback")

    def publish():
        pairing.save_owner(path, original, 42)

    monkeypatch.setattr(pairing, "transaction", uncertain_transaction)
    with pytest.raises(OSError, match="synthetic"):
        pairing.reserve_database_owner(
            Settings(database_url=db_engine.url.render_as_string(hide_password=False)),
            42,
            before_commit=publish,
        )

    assert path.read_bytes() != original
    assert "GA_TELEGRAM_USER_ID='42'" in path.read_text()
    monkeypatch.setattr(pairing, "transaction", real_transaction)
    pairing.reserve_database_owner(
        Settings(database_url=db_engine.url.render_as_string(hide_password=False)), 42
    )
    db.expire_all()
    assert db.scalar(select(ChannelBinding.external_id)) == "42"
