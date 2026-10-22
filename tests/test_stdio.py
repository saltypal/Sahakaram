"""Exercise the actual subprocess transport rather than calling a handler directly."""

import asyncio
import json
import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from sahakaram import __version__


@pytest.mark.asyncio
async def test_official_client_discovers_and_calls_stdio_tool() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sahakaram", "mcp"],
        env={
            **os.environ,
            "SAHAKARAM_INSTANCE_NAME": "Integration A",
            "SAHAKARAM_AUTO_START_CONTROL": "false",
        },
    )
    async with asyncio.timeout(20):
        async with Client(parameters) as client:
            assert client.server_info is not None
            assert client.server_info.name == "Sahakaram"
            assert client.server_info.version == __version__
            tools = await client.list_tools()
            assert {
                "get_status",
                "create_room",
                "join_room",
                "send_message",
                "receive_messages",
                "create_task",
                "assign_task",
                "update_task",
                "leave_room",
            }.issubset({tool.name for tool in tools.tools})
            result = await client.call_tool("get_status", {})
            assert not result.is_error
            assert result.structured_content == {
                "service": "Sahakaram",
                "instance_name": "Integration A",
                "version": __version__,
                "phase": 3,
                "transport": "stdio",
                "implemented_features": [
                    "status",
                    "local_control",
                    "codex_adapter",
                    "sqlite",
                    "local_rooms",
                ],
            }


@pytest.mark.asyncio
async def test_protocol_stdout_is_json_and_logging_stays_on_stderr() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sahakaram",
        "mcp",
        env={**os.environ, "SAHAKARAM_AUTO_START_CONTROL": "false"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "sahakaram-protocol-check", "version": "1"},
        },
    }
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate((json.dumps(request) + "\n").encode()), timeout=20
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    assert process.returncode == 0, stderr.decode()
    responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert any(response.get("id") == 1 and "result" in response for response in responses)
    assert "Starting Phase 3 MCP server" in stderr.decode()


@pytest.mark.asyncio
async def test_invalid_configuration_fails_without_protocol_noise() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sahakaram",
        "mcp",
        env={**os.environ, "SAHAKARAM_LOG_LEVEL": "invalid-sensitive-value"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    assert process.returncode == 2
    assert stdout == b""
    assert b"log_level" in stderr
    assert b"invalid-sensitive-value" not in stderr
