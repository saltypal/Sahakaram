"""Registry and local HTTP security regression checks."""

from pathlib import Path

from fastapi.testclient import TestClient

from sahakaram.local.registry import Registry
from sahakaram.local_api import create_local_app

PASSWORD = "local test passphrase"


def test_registry_credentials_and_expiry(tmp_path: Path) -> None:
    registry = Registry(tmp_path, ttl=-1)
    registry.create("Alice", PASSWORD)
    assert PASSWORD not in registry.path.read_text()
    assert not registry.authorized(registry.unlock(PASSWORD))
    other = Registry(tmp_path)
    token = other.unlock(PASSWORD)
    assert other.authorized(token)
    other.lock()
    assert not other.authorized(token)


def test_local_api_security_and_account_isolation(tmp_path: Path) -> None:
    with TestClient(create_local_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/accounts").status_code == 401
        assert (
            client.post(
                "/api/profile", json={"displayname": "Alice", "passphrase": PASSWORD}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/profile", json={"displayname": "Alice", "passphrase": PASSWORD}
            ).status_code
            == 400
        )
        assert client.post("/api/unlock", json={"passphrase": "wrong"}).status_code == 401
        token = client.post("/api/unlock", json={"passphrase": PASSWORD}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        accounts = [
            client.post("/api/accounts", headers=headers, json={"label": str(i)}).json()
            for i in range(2)
        ]
        assert accounts[0]["codex_home"] != accounts[1]["codex_home"]
        assert Path(accounts[0]["codex_home"]).is_relative_to(tmp_path)
        assert not list(Path(accounts[0]["codex_home"]).iterdir())
        assert (
            client.post(
                "/api/accounts",
                headers={**headers, "Origin": "http://evil.test"},
                json={"label": "evil"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/accounts",
                headers={**headers, "Origin": "http://127.0.0.1"},
                json={"label": "missing csrf"},
            ).status_code
            == 403
        )
        assert client.get("/healthz", headers={"Host": "evil.test"}).status_code == 403
        assert (
            client.post(
                f"/api/accounts/{accounts[0]['id']}/sessions",
                headers=headers,
                json={"workspace": "relative"},
            ).status_code
            == 400
        )
        assert client.post("/api/lock", headers=headers).status_code == 200
        assert client.get("/api/accounts", headers=headers).status_code == 401


def test_backend_unavailable_never_reports_success(tmp_path: Path) -> None:
    from sahakaram.adapters.codex import CodexUnavailable

    def unavailable(home: Path) -> None:
        raise CodexUnavailable("backend unavailable")

    with TestClient(
        create_local_app(tmp_path, codex_factory=unavailable), base_url="http://127.0.0.1"
    ) as client:
        client.post("/api/profile", json={"displayname": "Alice", "passphrase": PASSWORD})
        token = client.post("/api/unlock", json={"passphrase": PASSWORD}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        account = client.post("/api/accounts", headers=headers, json={"label": "one"}).json()
        response = client.post(
            f"/api/accounts/{account['id']}/sessions",
            headers=headers,
            json={"workspace": str(tmp_path)},
        )
        assert response.status_code == 503
        assert "thread_id" not in response.json()
