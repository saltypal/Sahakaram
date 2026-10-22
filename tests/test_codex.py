"""Coordinator contract tests use deterministic adapters; no live inference."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sahakaram.local.coordinator import Coordinator
from sahakaram.local.registry import Registry


class FakeCodex:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.maximum = 0
        self.closed = False

    async def login(self) -> dict[str, Any]:
        return {"status": "login-required"}

    async def usage(self) -> dict[str, Any]:
        return {"available": False, "reason": "not authenticated"}

    async def start(self, workspace: Path, sandbox: str) -> str:
        assert sandbox in {"read-only", "workspace-write"}
        return "owned-thread"

    async def resume(self, thread_id: str, workspace: Path) -> None:
        pass

    async def turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        await self.release.wait()
        self.active -= 1
        return {"status": "completed", "final_response": prompt}

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        self.release.set()
        return {"status": "interrupt-requested"}

    async def close(self) -> None:
        self.closed = True


async def test_serial_turns_interrupt_and_ownership(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state")
    first = registry.add_account("one")["id"]
    second = registry.add_account("two")["id"]
    coordinator = Coordinator(registry, FakeCodex)
    thread = await coordinator.start(first, str(tmp_path), "read-only")
    with pytest.raises(KeyError):
        await coordinator.turn(second, thread, "must fail")
    with pytest.raises(ValueError):
        await coordinator.start(first, "relative", "read-only")
    adapter = coordinator.adapter(first)
    assert isinstance(adapter, FakeCodex)
    a = asyncio.create_task(coordinator.turn(first, thread, "a"))
    await adapter.entered.wait()
    b = asyncio.create_task(coordinator.turn(first, thread, "b"))
    interrupted = await asyncio.wait_for(coordinator.interrupt(first, thread), 1)
    assert interrupted["status"] == "interrupt-requested"
    await asyncio.gather(a, b)
    assert adapter.maximum == 1
    assert (await adapter.usage())["available"] is False
    await coordinator.close()
    assert adapter.closed


async def test_adapter_missing_sdk_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sahakaram.adapters.codex import CodexAdapter, CodexUnavailable

    def absent(name: str) -> None:
        raise ImportError("test unavailable")

    monkeypatch.setattr("sahakaram.adapters.codex.importlib.import_module", absent)
    with pytest.raises(CodexUnavailable, match="sahakaram\\[codex\\]"):
        await CodexAdapter(tmp_path).usage()


async def test_adapter_refuses_unsupported_controls(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from sahakaram.adapters.codex import CodexAdapter, CodexUnavailable

    adapter = CodexAdapter(tmp_path)
    adapter.sdk = SimpleNamespace(Sandbox=SimpleNamespace())
    with pytest.raises(CodexUnavailable, match="sandbox"):
        adapter.sandbox("read-only")
    with pytest.raises(CodexUnavailable, match="approvals"):
        adapter.approvals()
    with pytest.raises(ValueError, match="Unsupported sandbox"):
        adapter.sandbox("full-access")


async def test_adapter_sdk_boundary_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from sahakaram.adapters.codex import CodexAdapter

    seen: dict[str, Any] = {}

    class Thread:
        id = "sdk-thread"

    class Client:
        def __init__(self, *, config: Any) -> None:
            seen["config"] = config

        async def __aenter__(self) -> "Client":
            return self

        async def account(self) -> Any:
            return SimpleNamespace(account=None)

        async def thread_start(self, **kwargs: Any) -> Thread:
            seen["start"] = kwargs
            return Thread()

        async def close(self) -> None:
            seen["closed"] = True

    sdk = SimpleNamespace(
        AsyncCodex=Client,
        CodexConfig=lambda **kwargs: kwargs,
        Sandbox=SimpleNamespace(read_only="safe-read"),
        ApprovalMode=SimpleNamespace(deny_all="deny"),
    )
    monkeypatch.setattr("sahakaram.adapters.codex.importlib.import_module", lambda name: sdk)
    adapter = CodexAdapter(tmp_path, codex_bin="explicit-runtime")
    assert (await adapter.usage()) == {
        "available": False,
        "authenticated": False,
        "reason": "limits-capability-unavailable",
    }
    assert await adapter.start(tmp_path, "read-only") == "sdk-thread"
    assert seen["start"]["sandbox"] == "safe-read"
    assert seen["start"]["approval_mode"] == "deny"
    assert seen["config"]["env"]["CODEX_HOME"] == str(tmp_path.resolve())
    assert seen["config"]["env"]["OPENAI_API_KEY"] == ""
    assert seen["config"]["codex_bin"] == "explicit-runtime"
    assert 'cli_auth_credentials_store="file"' in seen["config"]["config_overrides"]
    await adapter.close()
    assert seen["closed"] is True


async def test_limits_same_transport_normalization_and_unknown(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from sahakaram.adapters.codex import CodexAdapter

    payload: dict[str, Any] = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000},
        },
        "rateLimitsByLimitId": {
            "review": {
                "limitName": "Review",
                "secondary": {"usedPercent": 80, "windowDurationMins": 10080, "resetsAt": None},
            }
        },
        "ordinaryUsageAllowed": None,
    }
    seen: list[str] = []

    async def request(method: str, params: Any, *, response_model: Any) -> Any:
        seen.append(method)
        assert params is None
        return response_model.model_validate(payload)

    adapter = CodexAdapter(tmp_path)
    client = SimpleNamespace(_client=SimpleNamespace(request=request))
    limits = await adapter._limits(client, True)
    assert limits["available"] is True
    assert limits["buckets"]["codex"]["windows"]["primary"] == {
        "used_percent": 25,
        "window_duration_minutes": 300,
        "resets_at_unix_seconds": 1800000000,
    }
    assert limits["buckets"]["review"]["windows"]["secondary"]["resets_at_unix_seconds"] is None
    assert limits["ordinary_usage_allowed"] is None
    assert seen == ["account/rateLimits/read"]
    payload.clear()
    payload["rateLimits"] = {"unknown": "future schema"}
    assert (await adapter._limits(client, True))["reason"] == "limits-response-unrecognized"


async def test_limits_auth_failure_and_signature_drift(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from sahakaram.adapters.codex import CodexAdapter

    async def request(method: str, params: Any, *, response_model: Any) -> Any:
        raise RuntimeError("sensitive transport text must not escape")

    adapter = CodexAdapter(tmp_path)
    client = SimpleNamespace(_client=SimpleNamespace(request=request))
    result = await adapter._limits(client, False)
    assert result == {
        "available": False,
        "authenticated": False,
        "reason": "authentication-required",
    }
    assert (await adapter._limits(client, True))["reason"] == "limits-request-failed"
    client._client.request = lambda: None
    assert (await adapter._limits(client, True))["reason"] == "limits-capability-incompatible"


@pytest.mark.parametrize("during_start", [True, False])
async def test_cancel_closes_runtime_before_releasing_turn(
    tmp_path: Path, during_start: bool
) -> None:
    from types import SimpleNamespace

    from sahakaram.adapters.codex import CodexAdapter

    entered, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Handle:
        id = "turn"

        async def run(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    class Thread:
        async def turn(self, *args: Any, **kwargs: Any) -> Handle:
            if during_start:
                entered.set()
                await asyncio.Event().wait()
            return Handle()

    class Client:
        async def close(self) -> None:
            closing.set()
            await release_close.wait()

    adapter = CodexAdapter(tmp_path)
    adapter.client = Client()
    adapter.sdk = SimpleNamespace(
        Sandbox=SimpleNamespace(read_only="safe"), ApprovalMode=SimpleNamespace(deny_all="deny")
    )
    adapter.threads["thread"] = Thread()
    adapter.sandboxes["thread"] = "read-only"
    task = asyncio.create_task(adapter.turn("thread", "prompt"))
    await entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.client is None
    assert not adapter.active


def test_api_session_listing_and_lock_closes_runtime(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from sahakaram.local_api import create_local_app

    app = create_local_app(tmp_path / "state", FakeCodex)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").json()["options"] == ["Join room", "Start room", "Manage Codex"]
        client.post("/api/profile", json={"displayname": "test", "passphrase": "test passphrase!"})
        token = client.post("/api/unlock", json={"passphrase": "test passphrase!"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        first = client.post("/api/accounts", json={"label": "one"}, headers=headers).json()["id"]
        second = client.post("/api/accounts", json={"label": "two"}, headers=headers).json()["id"]
        base = f"/api/accounts/{first}/sessions"
        assert (
            client.post(base, json={"workspace": str(tmp_path)}, headers=headers).status_code == 201
        )
        assert client.get(base, headers=headers).json() == [
            {"thread_id": "owned-thread", "workspace": str(tmp_path.resolve())}
        ]
        assert client.get(f"/api/accounts/{second}/sessions", headers=headers).json() == []
        assert client.get(base).status_code == 401
        backend = app.state.coordinator.adapters[first]
        assert not backend.closed
        assert client.post("/api/lock", headers=headers).status_code == 200
        assert backend.closed
        assert client.get(base, headers=headers).status_code == 401


async def test_lock_invalidates_waiting_account_operations(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state")
    account = registry.add_account("one")["id"]
    coordinator = Coordinator(registry, FakeCodex)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with coordinator.serial(account):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    queued = asyncio.create_task(coordinator.start(account, str(tmp_path), "read-only"))
    await asyncio.sleep(0)
    await coordinator.close()
    coordinator.activate()
    release.set()
    await holder
    with pytest.raises(PermissionError, match="Profile locked"):
        await queued
    assert not coordinator.adapters
