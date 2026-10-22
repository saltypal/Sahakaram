"""Standalone backend, SQLite transaction, and Dev Tunnels boundary checks."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import sqlalchemy as sa
from typer.testing import CliRunner

from sahakaram.cli import app
from sahakaram.storage import schema as s
from sahakaram.storage.database import write_transaction


def test_sqlite_pragmas_and_snapshot_allow_concurrent_writer(database):
    with database.connect() as reader:
        assert reader.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert reader.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        count = reader.execute(sa.select(sa.func.count()).select_from(s.users)).scalar_one()
        with write_transaction(database) as writer:
            writer.execute(sa.insert(s.users).values(id=uuid4(), displayname="Concurrent writer"))
        # The reader sees one coherent snapshot while the writer commits.
        assert reader.execute(sa.select(sa.func.count()).select_from(s.users)).scalar_one() == count
    with database.connect() as reader:
        assert (
            reader.execute(sa.select(sa.func.count()).select_from(s.users)).scalar_one()
            == count + 1
        )


def test_sqlite_utc_round_trip(database):
    user, account, snapshot = uuid4(), uuid4(), uuid4()
    observed = datetime(2026, 10, 22, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with write_transaction(database) as c:
        c.execute(sa.insert(s.users).values(id=user, displayname="A"))
        c.execute(
            sa.insert(s.accounts).values(id=account, user_id=user, provider="codex", label="A")
        )
        c.execute(
            sa.insert(s.usage_snapshots).values(
                id=snapshot,
                account_id=account,
                source="test",
                availability="unavailable",
                observed_at=observed,
            )
        )
    with database.connect() as c:
        saved = c.execute(sa.select(s.usage_snapshots.c.observed_at)).scalar_one()
    assert saved.tzinfo == UTC and saved == observed


def test_backend_boots_with_plugin_and_codex_imports_forbidden(tmp_path):
    script = """
import sys, importlib.abc
class NoPlugins(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'mcp', 'openai_codex'} or fullname in {
            'sahakaram.server', 'sahakaram.bridge', 'sahakaram.local_api'
        }:
            raise RuntimeError('Backend imported a client/runtime dependency: ' + fullname)
sys.meta_path.insert(0, NoPlugins())
from pathlib import Path
from fastapi.testclient import TestClient
from sahakaram.backend import create_backend
from sahakaram.config import Settings
app = create_backend(Settings(state_dir=Path(sys.argv[1])))
with TestClient(app) as client:
    assert client.get('/readyz').json() == {'status': 'ready'}
    key = (Path(sys.argv[1]) / 'host-bootstrap.key').read_text().strip()
    headers = {'X-Sahakaram-Bootstrap': key}
    identity = client.post('/api/v1/identities', headers=headers,
        json={'displayname':'Independent user', 'label':'HTTP client'}).json()
    room = client.post('/api/v1/rooms', headers=headers,
        json={**identity, 'name':'Independent room'})
    assert room.status_code == 200, room.text
assert (Path(sys.argv[1]) / 'sahakaram.sqlite3').exists()
assert 'sahakaram.server' not in sys.modules
"""
    env = {k: v for k, v in os.environ.items() if k != "SAHAKARAM_DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_share_uses_devtunnel_and_only_requested_port(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "shutil.which", lambda name: "/test/devtunnel" if name == "devtunnel" else None
    )

    def run(arguments, **kwargs):
        seen.append(arguments)
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("subprocess.run", run)
    assert CliRunner().invoke(app, ["share", "--port", "8788"]).exit_code == 0
    assert seen == [["/test/devtunnel", "host", "-p", "8788", "--allow-anonymous"]]
