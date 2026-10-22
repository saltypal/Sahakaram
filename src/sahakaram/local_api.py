"""Loopback-only local control API. Bearer tokens are memory-only unlock sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sahakaram.adapters.codex import CodexAdapter, CodexUnavailable
from sahakaram.bootstrap import state_identity
from sahakaram.local.coordinator import Adapter, Coordinator
from sahakaram.local.registry import Registry


class ProfileInput(BaseModel):
    displayname: str = Field(min_length=1, max_length=100)
    passphrase: str = Field(min_length=12, max_length=1024)


class UnlockInput(BaseModel):
    passphrase: str = Field(min_length=1, max_length=1024)


class AccountInput(BaseModel):
    label: str = Field(min_length=1, max_length=100)


class SessionInput(BaseModel):
    workspace: str = Field(min_length=1)
    sandbox: Literal["read-only", "workspace-write"] = "read-only"


class TurnInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=100000)


def create_local_app(
    state_dir: Path,
    codex_factory: Callable[[Path], Adapter] | None = None,
) -> FastAPI:
    registry = Registry(state_dir)
    coordinator = Coordinator(registry, codex_factory or CodexAdapter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        registry.lock()
        await coordinator.close()

    app = FastAPI(lifespan=lifespan)
    app.state.registry = registry
    app.state.coordinator = coordinator

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "Loopback Host required"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("x-sahakaram-csrf") != "1":
                    return JSONResponse({"detail": "CSRF header required"}, status_code=403)
        return await call_next(request)

    def authenticated(request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not registry.authorized(token):
            raise HTTPException(401, "Unlock required")

    @app.exception_handler(PermissionError)
    async def locked(request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": "Unlock required"}, status_code=401)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": "Account or owned session not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(CodexUnavailable)
    async def unavailable(request: Request, exc: CodexUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/")
    async def entry() -> dict[str, Any]:
        return {
            "service": "sahakaram-local",
            "state_id": state_identity(state_dir),
            "options": ["Join room", "Start room", "Manage Codex"],
        }

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/profile", status_code=201)
    async def profile(body: ProfileInput) -> dict[str, str]:
        registry.create(body.displayname, body.passphrase)
        return {"displayname": body.displayname}

    @app.post("/api/unlock")
    async def unlock(body: UnlockInput) -> dict[str, Any]:
        try:
            token = registry.unlock(body.passphrase)
        except PermissionError as exc:
            raise HTTPException(401, "Invalid credentials") from exc
        coordinator.activate()
        return {"token": token, "expires_in": registry.ttl}

    auth = [Depends(authenticated)]

    @app.post("/api/lock", dependencies=auth)
    async def lock() -> dict[str, bool]:
        registry.lock()
        await coordinator.close()
        return {"locked": True}

    @app.get("/api/accounts", dependencies=auth)
    async def accounts() -> list[dict[str, str]]:
        return registry.accounts()

    @app.post("/api/accounts", dependencies=auth, status_code=201)
    async def add_account(body: AccountInput) -> dict[str, str]:
        return registry.add_account(body.label)

    @app.get("/api/accounts/{account_id}/usage", dependencies=auth)
    async def usage(account_id: str) -> dict[str, Any]:
        return await coordinator.adapter(account_id).usage()

    @app.post("/api/accounts/{account_id}/login", dependencies=auth)
    async def login(account_id: str) -> dict[str, Any]:
        async with coordinator.serial(account_id):
            return await coordinator.adapter(account_id).login()

    @app.get("/api/accounts/{account_id}/sessions", dependencies=auth)
    async def sessions(account_id: str) -> list[dict[str, str]]:
        return registry.sessions(account_id)

    @app.post("/api/accounts/{account_id}/sessions", dependencies=auth, status_code=201)
    async def start(account_id: str, body: SessionInput) -> dict[str, str]:
        thread = await coordinator.start(account_id, body.workspace, body.sandbox)
        return {"thread_id": thread}

    @app.post("/api/accounts/{account_id}/sessions/{thread_id}/resume", dependencies=auth)
    async def resume(account_id: str, thread_id: str) -> dict[str, bool]:
        await coordinator.resume(account_id, thread_id)
        return {"resumed": True}

    @app.post("/api/accounts/{account_id}/sessions/{thread_id}/turn", dependencies=auth)
    async def turn(account_id: str, thread_id: str, body: TurnInput) -> dict[str, Any]:
        return await coordinator.turn(account_id, thread_id, body.prompt)

    @app.post("/api/accounts/{account_id}/sessions/{thread_id}/interrupt", dependencies=auth)
    async def interrupt(account_id: str, thread_id: str) -> dict[str, Any]:
        return await coordinator.interrupt(account_id, thread_id)

    return app
