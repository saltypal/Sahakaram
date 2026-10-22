"""Local identity and isolated account registry (no provider secrets)."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from filelock import FileLock


class Registry:
    def __init__(self, state_dir: Path, ttl: float = 3600) -> None:
        self.root = state_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "local.json"
        self.file_lock = FileLock(str(self.path) + ".lock")
        self.tokens: dict[str, float] = {}
        self.ttl = ttl
        self.hasher = PasswordHasher()

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"profile": None, "accounts": {}, "sessions": {}}
        return dict(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, displayname: str, passphrase: str) -> None:
        with self.file_lock:
            data = self.read()
            if data["profile"]:
                raise ValueError("Profile already exists")
            data["profile"] = {"displayname": displayname, "verifier": self.hasher.hash(passphrase)}
            self.write(data)

    def unlock(self, passphrase: str) -> str:
        with self.file_lock:
            profile = self.read()["profile"]
        try:
            valid = profile and self.hasher.verify(profile["verifier"], passphrase)
        except VerificationError:
            valid = False
        if not valid:
            raise PermissionError("Invalid credentials")
        token = secrets.token_urlsafe(32)
        self.tokens[token] = time.monotonic() + self.ttl
        return token

    def authorized(self, token: str) -> bool:
        expires = self.tokens.get(token, 0)
        if expires <= time.monotonic():
            self.tokens.pop(token, None)
            return False
        return True

    def lock(self) -> None:
        self.tokens.clear()

    def accounts(self) -> list[dict[str, str]]:
        with self.file_lock:
            return list(self.read()["accounts"].values())

    def account(self, account_id: str) -> dict[str, str]:
        with self.file_lock:
            data = self.read()["accounts"].get(account_id)
        if data is None:
            raise KeyError("Unknown account")
        return dict(data)

    def add_account(self, label: str) -> dict[str, str]:
        account_id = uuid4().hex
        home = self.root / "accounts" / account_id / "codex"
        home.mkdir(parents=True)
        account = {"id": account_id, "label": label, "codex_home": str(home)}
        with self.file_lock:
            data = self.read()
            data["accounts"][account_id] = account
            self.write(data)
        return account

    def remember(self, account_id: str, thread_id: str, workspace: str) -> None:
        with self.file_lock:
            data = self.read()
            data["sessions"].setdefault(account_id, {})[thread_id] = workspace
            self.write(data)

    def sessions(self, account_id: str) -> list[dict[str, str]]:
        self.account(account_id)
        with self.file_lock:
            sessions = self.read()["sessions"].get(account_id, {})
        return [
            {"thread_id": str(thread), "workspace": str(workspace)}
            for thread, workspace in sessions.items()
        ]

    def workspace(self, account_id: str, thread_id: str) -> str:
        with self.file_lock:
            result = self.read()["sessions"].get(account_id, {}).get(thread_id)
        if result is None:
            raise KeyError("Session does not belong to this account")
        return str(result)
