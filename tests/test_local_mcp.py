"""MCP tools traverse authenticated local HTTP without a provider inference call."""

from pathlib import Path
from uuid import uuid4

import httpx
from mcp import Client

from sahakaram.bridge import atomic_json
from sahakaram.config import Settings
from sahakaram.local_api import create_local_app
from sahakaram.server import create_server


class CodexStub:
    async def start(self, workspace, sandbox):
        return str(uuid4())

    async def usage(self):
        return {"available": False, "authenticated": False, "reason": "authentication-required"}

    async def turn(self, thread_id, prompt):
        return {"status": "completed", "final_response": "scripted response"}

    async def close(self):
        pass


async def test_local_mcp_unlock_owned_turn_and_relock(tmp_path: Path, monkeypatch):
    app = create_local_app(tmp_path, lambda home: CodexStub())
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=app)
        return real_client(**kwargs)

    monkeypatch.setattr("sahakaram.bridge.httpx.AsyncClient", client_factory)
    settings = Settings(state_dir=tmp_path, auto_start_control=False)
    async with app.router.lifespan_context(app):
        async with Client(create_server(settings)) as mcp:
            locked = await mcp.call_tool("list_accounts", {})
            assert locked.is_error
            async with client_factory(base_url="http://127.0.0.1:8787") as http:
                assert (
                    await http.post(
                        "/api/profile",
                        json={"displayname": "Alice", "passphrase": "local test passphrase"},
                    )
                ).status_code == 201
                session = (
                    await http.post("/api/unlock", json={"passphrase": "local test passphrase"})
                ).json()
                atomic_json(tmp_path / "control-session.json", session)
                added = await mcp.call_tool("add_account", {"label": "A"})
                assert not added.is_error
                account = added.structured_content["id"]
                usage = await mcp.call_tool("get_usage", {"account_id": account})
                assert usage.structured_content["reason"] == "authentication-required"
                started = await mcp.call_tool(
                    "start_session", {"account_id": account, "workspace": str(tmp_path)}
                )
                assert not started.is_error
                thread = started.structured_content["thread_id"]
                result = await mcp.call_tool(
                    "run_turn",
                    {"account_id": account, "thread_id": thread, "prompt": "scripted work"},
                )
                assert result.structured_content["final_response"] == "scripted response"
                assert session["token"] not in str(result)
                wrong = await mcp.call_tool(
                    "run_turn",
                    {"account_id": str(uuid4()), "thread_id": thread, "prompt": "wrong owner"},
                )
                assert wrong.is_error
                await http.post(
                    "/api/lock", headers={"Authorization": f"Bearer {session['token']}"}
                )
                assert (await mcp.call_tool("list_accounts", {})).is_error
