"""Room scenarios through HTTP plus direct PostgreSQL integrity/concurrency checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from sahakaram.hub import create_hub
from sahakaram.security import Tokens
from sahakaram.services import DomainError, Rooms
from sahakaram.storage import schema as s
from sahakaram.storage.database import create_database, migrate


@pytest.fixture
def service(database):
    return Rooms(database, Tokens("test-only-signing-key-" * 3))


@pytest.fixture
def client(database, service):
    with TestClient(create_hub(database, service, "test-bootstrap", recover=False)) as client:
        yield client


def authorization(token):
    return {"Authorization": f"Bearer {token}"}


def create_room(client, *, capacity=3):
    bootstrap = {"X-Sahakaram-Bootstrap": "test-bootstrap"}
    identity = client.post(
        "/api/v1/identities", headers=bootstrap, json={"displayname": "Owner", "label": "Account A"}
    )
    assert identity.status_code == 200, identity.text
    response = client.post(
        "/api/v1/rooms",
        headers=bootstrap,
        json={
            **identity.json(),
            "name": "Test room",
            "max_participants": capacity,
            "link_phrase": "a-long-room-linking-phrase",
        },
    )
    assert response.status_code == 200, response.text
    return {**identity.json(), **response.json()}


def invite(client, room, kind="agent"):
    response = client.post(
        "/api/v1/invitations", headers=authorization(room["admin_token"]), json={"kind": kind}
    )
    assert response.status_code == 200, response.text
    return response.json()


def join(client, invitation, label="B", **extras):
    return client.post(
        "/api/v1/join",
        json={"secret": invitation["secret"], "displayname": label, "label": label, **extras},
    )


def test_three_participant_conversation_task_review_and_replay(client, service):
    room = create_room(client)
    b = join(client, invite(client, room)).json()
    c = join(client, invite(client, room), "C").json()
    assert (
        len(
            client.get("/api/v1/room", headers=authorization(room["admin_token"])).json()[
                "participants"
            ]
        )
        == 3
    )
    lead = authorization(room["participant_token"])
    b_headers = authorization(b["participant_token"])
    message_id, task_id = str(uuid4()), str(uuid4())
    body = {
        "message_id": message_id,
        "content": "B implement; C review",
        "recipient_id": b["participant_id"],
    }
    posted = client.post("/api/v1/messages", headers=lead, json=body)
    assert posted.status_code == 200, posted.text
    cursor = posted.json()["sequence"] - 1
    duplicate = client.post("/api/v1/messages", headers=lead, json=body)
    assert duplicate.json()["replayed"] is True
    assert (
        client.post(
            "/api/v1/messages", headers=lead, json={**body, "content": "changed"}
        ).status_code
        == 409
    )
    task = client.post(
        "/api/v1/tasks", headers=lead, json={"task_id": task_id, "title": "Implement feature"}
    ).json()
    assert (
        client.post(
            "/api/v1/tasks",
            headers=b_headers,
            json={"task_id": str(uuid4()), "title": "Unauthorized task"},
        ).status_code
        == 403
    )
    task = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=lead,
        json={"version": task["version"], "participant_id": b["participant_id"]},
    ).json()
    task = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=b_headers,
        json={"version": task["version"], "status": "working"},
    ).json()
    assert task["status"] == "working"
    task = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=b_headers,
        json={"version": task["version"], "status": "review", "result_summary": "Tests pass"},
    ).json()
    assert (
        client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=b_headers,
            json={"version": task["version"], "status": "done"},
        ).status_code
        == 403
    )
    task = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=lead,
        json={"version": task["version"], "status": "done"},
    ).json()
    assert task["status"] == "done"
    events = client.get(
        f"/api/v1/events?after={cursor}", headers=authorization(c["participant_token"])
    ).json()
    messages = [e for e in events["events"] if e["kind"] == "message.created"]
    assert len(messages) == 1
    assert messages[0]["payload"]["content"] == body["content"]
    replayed = client.get(f"/api/v1/events?after={events['next_cursor']}", headers=b_headers).json()
    assert replayed["events"] == []


def test_concurrent_last_seat_and_invite_consumption(client, service):
    room = create_room(client, capacity=2)
    invitations = [invite(client, room), invite(client, room)]

    def admit(item):
        try:
            return service.join(item["secret"], "Peer", "Peer")
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(admit, invitations))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "ROOM_FULL" in results
    with service.engine.connect() as c:
        assert sum(c.execute(sa.select(s.invitations.c.use_count)).scalars()) == 1


def test_same_user_multiple_accounts_and_invalid_link(client):
    room = create_room(client)
    invitation = invite(client, room)
    failed = join(
        client, invitation, existing_user_id=room["user_id"], link_phrase="wrong-long-phrase"
    )
    assert failed.status_code == 401
    admitted = join(
        client,
        invitation,
        existing_user_id=room["user_id"],
        link_phrase="a-long-room-linking-phrase",
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["user_id"] == room["user_id"]
    assert admitted.json()["account_id"] != room["account_id"]
    assert join(client, invitation).status_code == 403


def test_expired_invite_kick_pause_and_authentication(client, database):
    room = create_room(client)
    invitation = invite(client, room)
    with database.begin() as c:
        c.execute(
            sa.update(s.invitations)
            .where(s.invitations.c.id == UUID(invitation["invitation_id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert join(client, invitation).status_code == 403
    b = join(client, invite(client, room)).json()
    assert (
        client.post(
            f"/api/v1/participants/{b['participant_id']}/kick",
            headers=authorization(room["participant_token"]),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/participants/{b['participant_id']}/kick",
            headers=authorization(room["admin_token"]),
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/room", headers=authorization(b["participant_token"])).status_code == 401
    )
    assert client.get("/api/v1/room").status_code == 401
    assert (
        client.post(
            "/api/v1/room/state",
            headers=authorization(room["admin_token"]),
            json={"status": "paused"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/messages",
            headers=authorization(room["participant_token"]),
            json={"message_id": str(uuid4()), "content": "Should fail"},
        ).status_code
        == 409
    )


def test_cross_room_refs_rejected_and_users_exactly_two_columns(client, database):
    first, second = create_room(client), create_room(client)
    assert set(s.users.c.keys()) == {"id", "displayname"}
    response = client.post(
        "/api/v1/messages",
        headers=authorization(first["participant_token"]),
        json={
            "message_id": str(uuid4()),
            "content": "Wrong room",
            "recipient_id": second["participant_id"],
        },
    )
    assert response.status_code == 422
    with pytest.raises(IntegrityError), database.begin() as c:
        c.execute(
            sa.insert(s.messages).values(
                room_id=UUID(first["room_id"]),
                sender_participant_id=UUID(first["participant_id"]),
                content="Cross-room FK",
                recipient_participant_id=UUID(second["participant_id"]),
            )
        )


def test_racing_claims_and_stale_versions(client, service):
    room = create_room(client)
    peer = join(client, invite(client, room)).json()
    lead = service.authenticate(room["participant_token"])
    actor = service.authenticate(peer["participant_token"])
    task_id = uuid4()
    task = service.create_task(lead, task_id, "Race")
    task = service.task_update(
        lead, task_id, task["version"], participant_id=UUID(peer["participant_id"])
    )

    def claim(_):
        try:
            return service.task_update(actor, task_id, task["version"], status="working")["status"]
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(claim, range(2)))
    assert sorted(outcomes) == ["VERSION_CONFLICT", "working"]


def test_restart_pauses_and_persists_events(client, database, service):
    room = create_room(client)
    headers = authorization(room["admin_token"])
    before = client.get("/api/v1/events", headers=headers).json()
    migrate(database)  # Upgrade is repeatable and preserves records.
    with TestClient(create_hub(database, service, "test-bootstrap", recover=True)) as restarted:
        snapshot = restarted.get("/api/v1/room", headers=headers).json()
        assert snapshot["room"]["status"] == "paused"
        events = restarted.get("/api/v1/events", headers=headers).json()["events"]
        assert events[: len(before["events"])] == before["events"]
        assert events[-1]["kind"] == "room.recovery_required"


def test_websocket_replays_committed_events(client):
    room = create_room(client)
    with client.websocket_connect(
        "/api/v1/events/ws?after=0", headers=authorization(room["admin_token"])
    ) as socket:
        batch = socket.receive_json()
        assert batch["events"][0]["kind"] == "room.created"


def test_usage_windows_are_separate_and_unknown_not_zero(client, database):
    room = create_room(client)
    headers = authorization(room["participant_token"])
    response = client.post(
        "/api/v1/usage",
        headers=headers,
        json={
            "availability": "available",
            "windows": [
                {"limit_id": "codex", "window_key": "primary", "used_percent": 20},
                {"limit_id": "codex", "window_key": "secondary", "used_percent": 50},
            ],
        },
    )
    assert response.status_code == 200, response.text
    observation = client.get("/api/v1/room", headers=headers).json()["usage"]
    assert len(observation) == 1 and len(observation[0]["windows"]) == 2
    assert (
        client.post(
            "/api/v1/usage", headers=headers, json={"availability": "unavailable"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/usage", headers=headers, json={"availability": "available"}
        ).status_code
        == 422
    )
    with database.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.usage_windows)).scalar_one() == 2


def test_other_databases_are_rejected():
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        create_database("sqlite:///not-supported.db")


def test_human_departure_releases_owned_seat_and_revokes_agent(client):
    room = create_room(client, capacity=2)
    peer = join(client, invite(client, room)).json()
    assert (
        client.post("/api/v1/leave", headers=authorization(peer["member_token"])).status_code == 200
    )
    assert (
        client.get("/api/v1/room", headers=authorization(peer["participant_token"])).status_code
        == 401
    )
    assert join(client, invite(client, room), "replacement").status_code == 200


def test_room_binding_rejects_wrong_room_header(client):
    room = create_room(client)
    response = client.get(
        "/api/v1/room",
        headers={**authorization(room["participant_token"]), "X-Sahakaram-Room": str(uuid4())},
    )
    assert response.status_code == 403


async def test_mcp_share_usage_converts_units_and_persists_observation(
    database, service, tmp_path, monkeypatch
):
    import httpx
    from mcp import Client

    from sahakaram.bridge import LocalBridge
    from sahakaram.config import Settings
    from sahakaram.server import create_server

    identity = service.register("Owner", "A")
    room = service.create(
        UUID(identity["user_id"]), UUID(identity["account_id"]), "Usage room", "", 3, None
    )

    async def observation(self, method, route, data=None):
        return {
            "available": True,
            "buckets": {
                "codex": {
                    "windows": {
                        "primary": {
                            "used_percent": 73,
                            "window_duration_minutes": 300,
                            "resets_at_unix_seconds": 1800000000,
                        }
                    }
                }
            },
        }

    monkeypatch.setattr(LocalBridge, "request", observation)
    real_client = httpx.AsyncClient
    app = create_hub(database, service, "test-bootstrap", recover=False)

    def client_factory(**kwargs):
        return real_client(**kwargs, transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr("sahakaram.bridge.httpx.AsyncClient", client_factory)
    settings = Settings(
        state_dir=tmp_path, auto_start_control=False, room_token=room["participant_token"]
    )
    async with Client(create_server(settings)) as client:
        result = await client.call_tool(
            "share_usage", {"room_id": room["room_id"], "account_id": str(uuid4())}
        )
        assert not result.is_error
    saved = service.snapshot(service.authenticate(room["participant_token"]))["usage"][0]
    window = saved["windows"][0]
    assert saved["source"] == "companion"
    assert window["used_percent"] == 73 and window["duration_seconds"] == 18000
    assert window["resets_at"].timestamp() == 1800000000
