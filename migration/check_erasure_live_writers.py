"""Check that erasure fences active writers and stale sessions on disposable storage."""

import argparse
import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from check_mcp_diary_create import REPORT, create, open_client
from check_web_chat_model import HEADERS, TOKENS
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner

INSTANCE = "source:synthetic:erasure"
ERASE = ("erase-all", "--confirm", "ERASE ALL LOCAL HEALTH DATA")
SOURCE = """
import json
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
reply = {"request_id": request["request_id"], "version": 1}
if request["method"] == "describe":
    reply["result"] = {
        "kind": "source", "instance_id": request["instance_id"],
        "implementation_version": "synthetic-erasure",
        "capabilities": {"observations": True, "cursor": True, "max_page_size": 2},
    }
else:
    assert request["method"] == "read_page"
    Path("page-ready").touch()
    until = time.monotonic() + 2.9
    while not Path("return-page").exists() and time.monotonic() < until:
        time.sleep(0.005)
    assert Path("return-page").exists()
    start, end = request["params"]["start"], request["params"]["end"]
    reply["result"] = {
        "instance_id": request["instance_id"], "page_kind": "partial",
        "fetched_at": end, "next_cursor": None,
        "records": [{
            "source_record_id": "synthetic-late-record", "operation": "upsert",
            "observed_at": start, "effective_at": start, "source_timezone": "UTC",
            "source_reference": "synthetic:late", "payload": {"value": 7},
        }],
    }
print(json.dumps(reply))
"""


def storage(engine, root):
    data, tokens, locks, backup = (root / name for name in ("data", "tokens", "locks", "backups"))
    for path in (data, tokens, backup):
        path.mkdir(mode=0o700)
        (path / "fixture").write_text("Synthetic fixture", encoding="utf-8")
    (data / "raw").mkdir(mode=0o700)
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(data),
        "GA_TOKEN_DIR": str(tokens),
        "GA_LOCK_DIR": str(locks),
        "GA_BACKUP_DIR": str(backup),
        "GA_API_TOKENS": json.dumps(TOKENS),
        "GA_MCP_ENABLE_WRITES": "true",
    }
    return env, data, tokens, locks, backup


@contextmanager
def running(binary, root, env, *arguments):
    with subprocess.Popen(
        [binary, *arguments], cwd=root, env=env, stdout=subprocess.PIPE, text=True
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise


def command(binary, root, env, *arguments):
    return subprocess.run(
        [binary, *arguments],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


async def wait_for(path, process):
    async with asyncio.timeout(2):
        while not path.exists():
            assert process.returncode is None
            await asyncio.sleep(0.01)


async def begin_source(binary, root, plugin, env):
    for marker in ("page-ready", "return-page"):
        (root / marker).unlink(missing_ok=True)
    return await asyncio.create_subprocess_exec(
        binary,
        "poll-source-extension",
        plugin,
        INSTANCE,
        cwd=root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def finish_source(root, process):
    (root / "return-page").touch()
    try:
        return await asyncio.wait_for(process.communicate(), 5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def verify(binary, engine, root):
    env, data, tokens, locks, backup = storage(engine, root)
    with Session(engine) as db, db.begin():
        first_owner = owner(db).id
    with running(binary, root, env, "worker") as worker:
        assert await asyncio.to_thread(worker.stdout.readline) == "Garmin AI worker ready\n"
        blocked = await asyncio.to_thread(command, binary, root, env, *ERASE)
        assert blocked.returncode == 1 and "Stop the runtime" in blocked.stderr
        assert not (locks / "erased").exists()

    with running(binary, root, env, "serve", "--port", "0") as server:
        url = (
            (await asyncio.to_thread(server.stdout.readline)).strip().removeprefix("Listening on ")
        )
        assert url.startswith("http://127.0.0.1:")
        async with (
            httpx.AsyncClient(base_url=url, trust_env=False, timeout=5) as http,
            open_client(str(binary), ["mcp"], root, env, "before-erasure") as old,
        ):
            assert (await http.get("/health/ready", headers=HEADERS)).status_code == 200
            assert not (await create(old, "synthetic-before-erasure"))["isError"]
            assert (await http.post("/events", json=REPORT, headers=HEADERS)).status_code in (
                200,
                201,
            )

            plugin = root / "synthetic-source"
            plugin.write_text(f"#!{sys.executable}\n" + SOURCE, encoding="utf-8")
            plugin.chmod(0o700)
            extension_env = {
                **env,
                "GA_INTEGRATIONS": json.dumps(
                    [
                        {
                            "id": INSTANCE,
                            "kind": "source",
                            "provider": "synthetic",
                        }
                    ]
                ),
            }
            source = await begin_source(binary, root, plugin, extension_env)
            try:
                await wait_for(root / "page-ready", source)
                erased = await asyncio.to_thread(command, binary, root, env, *ERASE)
                assert erased.returncode == 0, erased.stderr
                assert json.loads(erased.stdout)["erased"] is True
                assert (locks / "erased").is_file() and not data.exists() and not tokens.exists()
            finally:
                output, error = await finish_source(root, source)
            assert source.returncode == 1 and not output, (output, error)
            assert b"storage" in error.lower(), error
            assert (await http.post("/events", json=REPORT, headers=HEADERS)).status_code == 503
            assert (await http.get("/health/ready", headers=HEADERS)).status_code == 503
            late = await create(old, "synthetic-late-erasure")
            assert late["isError"]
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM people),(SELECT count(*) FROM events),"
                        "(SELECT count(*) FROM source_payloads),(SELECT count(*) FROM audit_log)"
                    )
                ).one() == (0, 0, 0, 0)

            retry = await asyncio.to_thread(command, binary, root, env, *ERASE)
            assert retry.returncode == 0 and json.loads(retry.stdout)["erased"]
            assert (backup / "fixture").read_text() == "Synthetic fixture"
            resumed = await asyncio.to_thread(command, binary, root, env, "resume-storage")
            assert resumed.returncode == 0, resumed.stderr
            with Session(engine) as db, db.begin():
                second_owner = owner(db).id
                assert second_owner != first_owner
            assert (await create(old, "synthetic-stale-owner"))["isError"]
            async with open_client(str(binary), ["mcp"], root, env, "after-erasure") as fresh:
                assert not (await create(fresh, "synthetic-new-owner"))["isError"]

            source = await begin_source(binary, root, plugin, extension_env)
            try:
                await wait_for(root / "page-ready", source)
                erased = await asyncio.to_thread(command, binary, root, env, *ERASE)
                assert erased.returncode == 0 and json.loads(erased.stdout)["erased"]
                resumed = await asyncio.to_thread(command, binary, root, env, "resume-storage")
                assert resumed.returncode == 0, resumed.stderr
                with Session(engine) as db, db.begin():
                    assert owner(db).id not in {first_owner, second_owner}
            finally:
                output, error = await finish_source(root, source)
            assert source.returncode == 1 and not output, (output, error)
            assert error.decode().strip() == "Source instance owner is unavailable", error
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM app_state "
                        "WHERE starts_with(key,'source-plugin:cursor:'))"
                    )
                ).one() == (0, 0)
            assert (backup / "fixture").read_text() == "Synthetic fixture"
    print(
        "Verified live HTTP and MCP, worker stop, old source pages after erase and resume, and backup preservation"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as root:
            asyncio.run(verify(args.binary.resolve(strict=True), engine, Path(root).resolve()))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
