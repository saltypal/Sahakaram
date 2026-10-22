"""Real MCP stdio clients traverse a subprocess HTTP hub into isolated SQLite."""

import asyncio
import json
import os
import socket
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from sqlalchemy import Engine, text

from sahakaram.bridge import atomic_json


def parameters(state: Path, hub_url: str) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if k != "SAHAKARAM_ROOM_TOKEN"}
    env.update(
        SAHAKARAM_STATE_DIR=str(state),
        SAHAKARAM_HUB_URL=hub_url,
        SAHAKARAM_AUTO_START_CONTROL="false",
    )
    return StdioServerParameters(command=sys.executable, args=["-m", "sahakaram", "mcp"], env=env)


async def call(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, f"{name} returned an MCP tool error"
    assert isinstance(result.structured_content, dict)
    value = result.structured_content

    def no_token_keys(item: Any) -> None:
        if isinstance(item, dict):
            assert not any(str(key).endswith("token") for key in item)
            for child in item.values():
                no_token_keys(child)
        elif isinstance(item, list):
            for child in item:
                no_token_keys(child)

    no_token_keys(value)
    return value


@pytest.mark.asyncio
async def test_mcp_room_transport_sqlite(database: Engine, tmp_path: Path) -> None:
    dsn = database.url
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    hub_url = f"http://127.0.0.1:{port}"
    hub_state = tmp_path / "host"
    environment = {
        **os.environ,
        "SAHAKARAM_STATE_DIR": str(hub_state),
        "SAHAKARAM_DATABASE_URL": dsn.render_as_string(hide_password=False),
        "SAHAKARAM_AUTO_START_CONTROL": "false",
    }
    with (tmp_path / "hub.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sahakaram",
            "hub",
            "--port",
            str(port),
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with (
                asyncio.timeout(40),
                httpx.AsyncClient(base_url=hub_url, trust_env=False, timeout=5) as http,
            ):
                for _ in range(200):
                    assert process.returncode is None, "Hub exited; see temporary hub.log"
                    try:
                        ready = await http.get("/readyz")
                        if ready.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("Hub readiness timed out; see temporary hub.log")
                bootstrap = {
                    "X-Sahakaram-Bootstrap": (hub_state / "host-bootstrap.key").read_text().strip()
                }
                identity = await http.post(
                    "/api/v1/identities",
                    headers=bootstrap,
                    json={"displayname": "Lead", "label": "lead"},
                )
                assert identity.status_code == 200
                created = await http.post(
                    "/api/v1/rooms",
                    headers=bootstrap,
                    json={**identity.json(), "name": "MCP integration", "max_participants": 3},
                )
                assert created.status_code == 200
                room = created.json()
                room_id = room["room_id"]
                lead_state = tmp_path / "lead"
                atomic_json(
                    lead_state / "room-sessions.json",
                    {room_id: {key: value for key, value in room.items() if key.endswith("token")}},
                )
                admin = {"Authorization": f"Bearer {room['admin_token']}"}
                peer_states = [tmp_path / "peer-one", tmp_path / "peer-two"]
                async with AsyncExitStack() as stack:
                    lead = await stack.enter_async_context(Client(parameters(lead_state, hub_url)))
                    peers = [
                        await stack.enter_async_context(Client(parameters(state, hub_url)))
                        for state in peer_states
                    ]
                    saved_tokens = [room["admin_token"], room["participant_token"]]
                    for index, (peer, state) in enumerate(zip(peers, peer_states, strict=True)):
                        invitation = await http.post(
                            "/api/v1/invitations", headers=admin, json={"kind": "agent"}
                        )
                        assert invitation.status_code == 200
                        joined = await call(
                            peer,
                            "join_room",
                            {
                                "invitation_secret": invitation.json()["secret"],
                                "displayname": f"Peer {index}",
                                "account_label": f"peer-{index}",
                            },
                        )
                        assert joined["room_id"] == room_id
                        stored = json.loads((state / "room-sessions.json").read_text())[room_id]
                        token = stored["participant_token"]
                        saved_tokens.append(token)
                        check = await http.get(
                            "/api/v1/room", headers={"Authorization": f"Bearer {token}"}
                        )
                        assert check.status_code == 200
                        assert any(
                            p["id"] == joined["participant_id"]
                            for p in check.json()["participants"]
                        )
                    message_id, task_id = str(uuid4()), str(uuid4())
                    message = await call(
                        lead,
                        "send_message",
                        {
                            "room_id": room_id,
                            "message_id": message_id,
                            "content": "Implement, then review.",
                        },
                    )
                    task = await call(
                        lead,
                        "create_task",
                        {
                            "room_id": room_id,
                            "task_id": task_id,
                            "title": "Transport integration evidence",
                        },
                    )
                    assert task["id"] == task_id
                    for peer in peers:
                        replay = await call(
                            peer,
                            "receive_messages",
                            {"room_id": room_id, "after_sequence": message["sequence"] - 1},
                        )
                        assert any(
                            event["kind"] == "message.created"
                            and event["payload"]["content"] == "Implement, then review."
                            for event in replay["events"]
                        )
                        assert any(event["kind"] == "task.created" for event in replay["events"])
                        snapshot = await call(peer, "get_room", {"room_id": room_id})
                        assert len(snapshot["participants"]) == 3
                        assert any(item["id"] == task_id for item in snapshot["tasks"])
                        encoded = json.dumps({"replay": replay, "snapshot": snapshot})
                        assert all(token not in encoded for token in saved_tokens)
                        empty = await call(
                            peer,
                            "receive_messages",
                            {"room_id": room_id, "after_sequence": replay["next_cursor"]},
                        )
                        assert empty["events"] == []
                # A fresh MCP process must recover the peer's saved credentials.
                async with Client(parameters(peer_states[0], hub_url)) as restarted:
                    snapshot = await call(restarted, "get_room", {"room_id": room_id})
                    assert snapshot["room"]["id"] == room_id
                with database.connect() as connection:
                    assert (
                        connection.execute(text("SELECT count(*) FROM participants")).scalar_one()
                        == 3
                    )
                    assert (
                        connection.execute(text("SELECT count(*) FROM messages")).scalar_one() == 1
                    )
                    assert connection.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 1
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
