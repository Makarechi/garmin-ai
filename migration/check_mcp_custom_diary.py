"""Compare MCP custom tracker writes and idempotency against pinned Python."""

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_custom_entries import MARKER, configure_metric, entry, specification
from check_mcp_diary_create import cancel_write, delay_write, open_client
from check_mcp_stdio import PYTHON_SERVER, document
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.definitions import activate_definition, create_definition_draft


async def record(client, key, value):
    return document(
        await client.call_tool("entries_create", {"entry": value, "idempotency_key": key})
    )


def counts(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("""
                SELECT (SELECT count(*) FROM events),
                  (SELECT count(*) FROM audit_log WHERE action='create' AND actor='mcp'),
                  (SELECT count(*) FROM metric_observations WHERE valid)
            """)
        ).one()


async def verify(binary, root, engine):
    with Session(engine) as session, session.begin():
        spec = specification(10, True)
        draft = create_definition_draft(session, spec, actor="synthetic", authorized=True)
        definition = activate_definition(
            session, draft.id, draft.revision, actor="synthetic", authorized=True
        )
        configure_metric(session, definition, 10)
        other = {
            **spec,
            "key": "user.synthetic_read_only",
            "fields": {
                name: {**field, "id": f"user.synthetic_read_only.{name}"}
                for name, field in spec["fields"].items()
            },
            "allowed_operations": ["query"],
        }
        draft = create_definition_draft(session, other, actor="synthetic", authorized=True)
        activate_definition(session, draft.id, draft.revision, actor="synthetic", authorized=True)

    report = entry(3, timezone="Europe/Bratislava")
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    async with open_client(str(binary), ["mcp"], root, env, "read-only") as native:
        assert "entries_create" not in [tool.name for tool in (await native.list_tools()).tools]
        denial = await record(native, "fixture-disabled-custom", report)
        assert denial["isError"] and "PermissionError" in denial["content"][0]["text"]
    assert counts(engine) == (0, 0, 0)

    env["GA_MCP_ENABLE_WRITES"] = "true"
    async with (
        open_client(sys.executable, ["-c", PYTHON_SERVER], root, env, "python") as python,
        open_client(str(binary), ["mcp"], root, env, "native") as native,
    ):
        descriptions = [
            {tool.name: document(tool) for tool in (await client.list_tools()).tools}
            for client in (python, native)
        ]
        assert descriptions[0]["entries_create"] == descriptions[1]["entries_create"]
        for key, value in [
            ("fixture-invalid-metric", entry(11)),
            ("fixture-invalid-unit", entry(3, unit="steps")),
            ("fixture-read-only", {**report, "definition_key": "user.synthetic_read_only"}),
        ]:
            expected = await record(python, key, value)
            actual = await record(native, key, value)
            assert actual == expected and actual["isError"], (key, expected, actual)
        assert counts(engine) == (0, 0, 0)

        for first, second, key in [
            (python, native, "fixture-python-custom"),
            (native, python, "fixture-native-custom"),
        ]:
            initial = await record(first, key, report)
            retry = await record(second, key, report)
            assert not initial["isError"] and not retry["isError"]
            assert initial["structuredContent"] == retry["structuredContent"]
            assert retry["structuredContent"]["payload"]["marker"] == MARKER
            assert retry["structuredContent"]["source"] == "mcp"
            conflict = await record(
                native, key, entry(3, marker=MARKER + 1, timezone="Europe/Bratislava")
            )
            assert conflict["isError"] and "Conflict" in conflict["content"][0]["text"]
        assert counts(engine) == (2, 2, 2)

        with delay_write(engine, "INSERT"):
            await cancel_write(
                binary,
                engine,
                root,
                env,
                "entries_create",
                {"entry": report, "idempotency_key": "fixture-cancelled-custom"},
            )
        assert counts(engine) == (2, 2, 2)
        recovered = await record(native, "fixture-cancelled-custom", report)
        assert not recovered["isError"]
        assert counts(engine) == (3, 3, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            asyncio.run(verify(args.binary.resolve(strict=True), Path(directory), engine))
    finally:
        engine.dispose()
    print("Verified MCP custom entry consent, schemas, cross-runtime retries, and cancellation")


if __name__ == "__main__":
    main()
