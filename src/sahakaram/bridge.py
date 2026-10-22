"""MCP-to-HTTP bridge. Tokens stay in local storage, not model-facing results."""

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from filelock import FileLock

from sahakaram.config import Settings


class BridgeError(Exception):
    pass


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        _write_json_unlocked(path, value)


def _write_json_unlocked(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_hub_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise BridgeError("INVALID_HOST: malformed host URL") from exc
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not loopback)
    ):
        raise BridgeError("INVALID_HOST: use an HTTPS origin, or HTTP loopback for local work")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise BridgeError("INVALID_HOST: invalid port") from exc
    return url.rstrip("/")


def save_connection(settings: Settings, url: str) -> None:
    atomic_json(settings.state_dir / "connection.json", {"hub_url": validate_hub_url(url)})


class RoomBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        saved = settings.state_dir / "connection.json"
        url = settings.hub_url
        if "SAHAKARAM_HUB_URL" not in os.environ and saved.exists():
            try:
                url = json.loads(saved.read_text(encoding="utf-8"))["hub_url"]
            except (ValueError, KeyError) as exc:
                raise BridgeError(
                    "INVALID_HOST: local connection configuration is invalid"
                ) from exc
        self.base = validate_hub_url(url)
        self.path = settings.state_dir / "room-sessions.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with FileLock(str(self.path) + ".lock"):
            return dict(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self, result: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize the read-modify-write, not only the final write.
        with FileLock(str(self.path) + ".lock"):
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            record = data.setdefault(result["room_id"], {})
            record.update({k: v for k, v in result.items() if k.endswith("token")})
            _write_json_unlocked(self.path, data)
        return {k: v for k, v in result.items() if not k.endswith("token")}

    def _headers(self, room_id: str, admin: bool) -> dict[str, str]:
        if self.settings.room_token:
            token = self.settings.room_token.get_secret_value()
        else:
            stored = self._load().get(room_id, {})
            token = (
                stored.get("admin_token")
                if admin
                else (
                    stored.get("participant_token")
                    or stored.get("member_token")
                    or stored.get("admin_token")
                )
            )
        if not token:
            raise BridgeError("AUTH_REQUIRED: enroll or configure SAHAKARAM_ROOM_TOKEN")
        return {"Authorization": f"Bearer {token}", "X-Sahakaram-Room": room_id}

    async def request(
        self,
        method: str,
        route: str,
        *,
        room_id: str | None = None,
        admin: bool = False,
        data: dict[str, Any] | None = None,
        bootstrap: bool = False,
    ) -> dict[str, Any]:
        saved = self.settings.state_dir / "connection.json"
        if "SAHAKARAM_HUB_URL" not in os.environ and saved.exists():
            self.base = validate_hub_url(json.loads(saved.read_text(encoding="utf-8"))["hub_url"])
        headers: dict[str, str] = {"Accept": "application/json"}
        if bootstrap:
            if urlsplit(self.base).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise BridgeError("FORBIDDEN: host bootstrap is loopback-only")
            key_path = self.settings.state_dir / "host-bootstrap.key"
            if not key_path.exists():
                raise BridgeError("HOST_NOT_RUNNING: start sahakaram hub first")
            session_path = self.settings.state_dir / "control-session.json"
            if not session_path.exists():
                raise BridgeError("AUTH_REQUIRED: unlock the local profile using sahakaram unlock")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                check = await client.get(
                    f"http://127.0.0.1:{self.settings.control_port}/api/accounts",
                    headers={"Authorization": f"Bearer {session['token']}"},
                )
                if check.status_code != 200:
                    raise BridgeError("AUTH_REQUIRED: local profile unlock expired")
            headers["X-Sahakaram-Bootstrap"] = key_path.read_text(encoding="utf-8").strip()
        elif room_id:
            headers.update(self._headers(room_id, admin))
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.request(
                    method, self.base + route, headers=headers, json=data
                )
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeError(
                "HOST_UNAVAILABLE: room host did not return a valid response"
            ) from exc
        if response.is_error:
            raise BridgeError(
                f"{result.get('code', 'REQUEST_FAILED')}: "
                f"{result.get('message', 'Host rejected request')}"
            )
        return dict(result)

    async def create(
        self, name: str, displayname: str, label: str, max_participants: int
    ) -> dict[str, Any]:
        identity = await self.request(
            "POST",
            "/api/v1/identities",
            bootstrap=True,
            data={"displayname": displayname, "label": label},
        )
        room = await self.request(
            "POST",
            "/api/v1/rooms",
            bootstrap=True,
            data={**identity, "name": name, "max_participants": max_participants},
        )
        return self._save(room)

    async def join(self, secret: str, displayname: str, label: str) -> dict[str, Any]:
        return self._save(
            await self.request(
                "POST",
                "/api/v1/join",
                data={"secret": secret, "displayname": displayname, "label": label},
            )
        )


class LocalBridge:
    """Only an existing, human-unlocked local session grants account control."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def request(self, method: str, route: str, data: dict[str, Any] | None = None) -> Any:
        path = self.settings.state_dir / "control-session.json"
        if not path.exists():
            raise BridgeError("AUTH_REQUIRED: run sahakaram unlock locally first")
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
                response = await client.request(
                    method,
                    f"http://127.0.0.1:{self.settings.control_port}" + route,
                    headers={"Authorization": f"Bearer {session['token']}"},
                    json=data,
                )
            result = response.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise BridgeError("LOCAL_UNAVAILABLE: local control request failed") from exc
        if response.is_error:
            raise BridgeError(f"LOCAL_REJECTED: {response.status_code}")
        return result
