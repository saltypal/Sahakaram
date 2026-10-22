"""Validate the friend-install surface and connection credential boundaries."""

import json
from pathlib import Path
from uuid import uuid4

import jwt
import pytest

from sahakaram.bridge import BridgeError, RoomBridge, save_connection, validate_hub_url
from sahakaram.config import Settings
from sahakaram.security import Actor, Tokens


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com/path",
        "file:///tmp/room",
        "https://example.com?token=secret",
        "https://example.com#fragment",
        "https://example.com:invalid",
        "not-a-url",
    ],
)
def test_room_connection_rejects_unsafe_origins(url):
    with pytest.raises(BridgeError):
        validate_hub_url(url)


def test_saved_connection_is_shared_with_mcp_and_env_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("SAHAKARAM_HUB_URL", raising=False)
    settings = Settings(state_dir=tmp_path)
    save_connection(settings, "https://example.com/")
    assert RoomBridge(settings).base == "https://example.com"
    assert "token" not in (tmp_path / "connection.json").read_text()
    monkeypatch.setenv("SAHAKARAM_HUB_URL", "http://127.0.0.1:9000")
    assert RoomBridge(Settings(state_dir=tmp_path)).base == "http://127.0.0.1:9000"


def test_explicit_demo_credential_lifetime_and_valid_signature():
    actor = Actor(kind="agent", id=uuid4(), room_id=uuid4(), version=0)
    tokens = Tokens("test-signing-key" * 4, ttl_minutes=120)
    encoded = tokens.issue(actor)
    assert tokens.read(encoded) == actor
    data = jwt.decode(encoded, options={"verify_signature": False})
    assert data["exp"] - data["iat"] == 7200
    with pytest.raises(ValueError):
        Tokens("test-signing-key" * 4, ttl_minutes=1441)


def test_plugin_wires_the_packaged_python_entrypoint():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
    mcp = json.loads((root / ".mcp.json").read_text())
    marketplace = json.loads((root / ".agents/plugins/marketplace.json").read_text())
    assert manifest["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["sahakaram"]["args"][-1] == "plugin-mcp"
    assert marketplace["plugins"][0]["source"]["path"] == "./"
