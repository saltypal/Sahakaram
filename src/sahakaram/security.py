"""Signed room credentials with current database revocation checks in the service."""

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import jwt
from filelock import FileLock
from pydantic import BaseModel


class Actor(BaseModel):
    kind: Literal["human", "agent"]
    id: UUID
    room_id: UUID
    version: int


def local_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32))
            path.chmod(0o600)
        secret = path.read_text(encoding="utf-8").strip()
        if len(secret) < 64:
            raise ValueError("Invalid local secret; restore it rather than silently rotating it")
        return secret


class Tokens:
    def __init__(self, key: str, *, ttl_minutes: int = 15) -> None:
        if len(key) < 32:
            raise ValueError("Signing key must have at least 32 characters")
        if not 1 <= ttl_minutes <= 1440:
            raise ValueError("Credential lifetime must be between 1 and 1440 minutes")
        self.ttl_minutes = ttl_minutes
        self.key = key

    def issue(self, actor: Actor) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                **actor.model_dump(mode="json"),
                "sub": str(actor.id),
                "aud": "sahakaram-room",
                "iss": "sahakaram",
                "iat": now,
                "exp": now + timedelta(minutes=self.ttl_minutes),
            },
            self.key,
            algorithm="HS256",
        )

    def read(self, token: str) -> Actor:
        payload = jwt.decode(
            token,
            self.key,
            algorithms=["HS256"],
            audience="sahakaram-room",
            issuer="sahakaram",
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
        return Actor.model_validate(payload)
