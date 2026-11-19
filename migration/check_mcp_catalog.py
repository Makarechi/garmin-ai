"""Compare the complete advertised MCP catalog through the frozen SDK."""

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_mcp_diary_create import open_client
from check_mcp_stdio import document
from mcp import McpError


async def capture(server, root, env, label):
    async with open_client(*server, root, env, label) as client:
        listed = document(await client.list_tools())
        invalid = [
            document(await client.call_tool(tool["name"], {"unexpected": "synthetic"}))
            for tool in listed["tools"]
        ]
        unsupported = []
        for call in (client.list_resources, client.list_resource_templates, client.list_prompts):
            try:
                await call()
            except McpError as error:
                unsupported.append(document(error.error))
            else:
                raise AssertionError("An unadvertised MCP family responded successfully")
        return listed, invalid, unsupported


async def compare(binary, root, engine):
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_TIMEZONE": "UTC",
    }
    reference = (sys.executable, ["-m", "garmin_ai.mcp_server"])
    native = (str(binary), ["mcp"])
    for enable_writes, count in ((False, 23), (True, 27)):
        if enable_writes:
            env["GA_MCP_ENABLE_WRITES"] = "true"
        expected, left, left_unsupported = await capture(reference, root, env, "frozen-python")
        actual, right, right_unsupported = await capture(native, root, env, "native")
        assert expected.keys() == actual.keys() == {"tools"}, (tuple(expected), tuple(actual))
        expected_tools = expected["tools"]
        actual_tools = actual["tools"]
        assert len(expected_tools) == len(actual_tools) == count
        for descriptor, candidate, invalid, reply in zip(
            expected_tools, actual_tools, left, right, strict=True
        ):
            name = descriptor["name"]
            assert descriptor == candidate, (name, descriptor, candidate)
            assert invalid == reply and reply["isError"], (name, invalid, reply)
        assert left_unsupported == right_unsupported, (left_unsupported, right_unsupported)
        assert all(error["code"] == -32601 for error in right_unsupported)
        print(f"Matched {count} MCP descriptors, invalid requests and unsupported methods")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            asyncio.run(compare(args.binary.resolve(strict=True), Path(directory), engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
