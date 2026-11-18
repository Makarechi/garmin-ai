"""Compare revision-checked partial corrections through the Python and Rust MCP servers."""

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_mcp_diary_create import REPORT, cancel_write, create, delay_write, open_client
from check_mcp_diary_delete import audits, permissions
from check_mcp_stdio import PYTHON_SERVER, document


async def update(client, event, revision, changes):
    return document(
        await client.call_tool(
            "events_update", {"event_id": event, "revision": revision, "changes": changes}
        )
    )


def normalize(entry):
    return {
        key: value
        for key, value in entry.items()
        if key
        not in {"id", "idempotency_key", "created_at", "updated_at", "recorded_at", "ingested_at"}
    }


async def verify(binary, root, engine):
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    missing = "00000000-0000-0000-0000-000000000001"
    async with open_client(str(binary), ["mcp"], root, env, "read-only") as native:
        assert "events_update" not in [tool.name for tool in (await native.list_tools()).tools]
        refusal = await update(native, missing, 1, {"timezone": "UTC"})
        assert refusal["isError"] and "PermissionError" in refusal["content"][0]["text"]

    env["GA_MCP_ENABLE_WRITES"] = "true"
    async with (
        open_client(sys.executable, ["-c", PYTHON_SERVER], root, env, "python") as python,
        open_client(str(binary), ["mcp"], root, env, "native") as native,
    ):
        descriptors = [
            {item.name: document(item) for item in (await client.list_tools()).tools}
            for client in (python, native)
        ]
        assert descriptors[0]["events_update"] == descriptors[1]["events_update"]
        original = {**REPORT, "original_text": "synthetic owner text", "confidence": 0.75}
        left = (await create(python, "fixture-update-python", original))["structuredContent"]
        right = (await create(python, "fixture-update-rust", original))["structuredContent"]
        correction = {
            "start": "2026-10-25T15:00:00+02:00",
            "end": None,
            "timezone": "Europe/Bratislava",
            "payload": {"description": "synthetic partial correction"},
        }
        for revision, patch in [
            (2, correction),
            (1, {}),
            (1, {"original_text": "forbidden"}),
            (1, {"payload": {"type": "exercise"}}),
            (1, {"payload": []}),
            (1, {"timezone": "invalid"}),
            (1, {"payload": {"unexpected": True}}),
        ]:
            expected = await update(python, right["id"], revision, patch)
            actual = await update(native, right["id"], revision, patch)
            assert actual == expected and actual["isError"], (revision, patch, expected, actual)
        assert len(audits(engine, right["id"])) == 1
        with delay_write(engine, "UPDATE"):
            await cancel_write(
                binary,
                engine,
                root,
                env,
                "events_update",
                {"event_id": right["id"], "revision": 1, "changes": correction},
            )
        assert len(audits(engine, right["id"])) == 1

        expected = await update(python, left["id"], 1, correction)
        actual = await update(native, right["id"], 1, correction)
        assert not expected["isError"] and not actual["isError"]
        corrected = actual["structuredContent"]
        assert normalize(corrected) == normalize(expected["structuredContent"])
        assert corrected["revision"] == 2 and corrected["payload"]["tags"] == ["fixture"]
        assert corrected["original_text"] == original["original_text"]
        assert corrected["confidence"] == original["confidence"]
        assert audits(engine, right["id"])[-1] == ("update", "mcp", right, corrected)

        results = await asyncio.gather(
            update(native, right["id"], 2, {"payload": {"description": "synthetic concurrent A"}}),
            update(native, right["id"], 2, {"payload": {"description": "synthetic concurrent B"}}),
        )
        assert sorted(item["isError"] for item in results) == [False, True]
        rejected = next(item for item in results if item["isError"])["content"][0]["text"]
        # The attempt may encounter the shared write lock before it can observe the newer revision.
        assert "Conflict" in rejected or "RuntimeError" in rejected, rejected
        stale_retry = await update(native, right["id"], 2, correction)
        assert stale_retry["isError"] and "Conflict" in stale_retry["content"][0]["text"]
        assert len(audits(engine, right["id"])) == 3

        permissions(engine, right["id"], "update")
        refused = await update(native, right["id"], 3, correction)
        assert refused["isError"] and "PermissionError" in refused["content"][0]["text"]
        assert len(audits(engine, right["id"])) == 3


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
    print(
        "Verified MCP partial corrections, preserved fields, cancellation, and concurrent revisions"
    )


if __name__ == "__main__":
    main()
