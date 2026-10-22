"""Authenticated local room HTTP/WebSocket transport for Phases 2–3."""

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from sahakaram.security import Actor
from sahakaram.services import DomainError, Rooms
from sahakaram.storage import schema as s


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IdentityInput(Input):
    displayname: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    user_id: UUID | None = None


class RoomInput(Input):
    user_id: UUID
    account_id: UUID
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(default="", max_length=8000)
    max_participants: int = Field(default=3, ge=1, le=100)
    link_phrase: str | None = Field(default=None, min_length=12, max_length=200)


class JoinInput(Input):
    secret: str = Field(min_length=20, max_length=200)
    displayname: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    existing_user_id: UUID | None = None
    link_phrase: str | None = Field(default=None, min_length=12, max_length=200)


class InviteInput(Input):
    kind: Literal["agent", "dashboard"] = "agent"
    ttl: int = Field(default=1800, ge=1, le=3600)


class MessageInput(Input):
    message_id: UUID
    content: str = Field(min_length=1, max_length=2000)
    kind: Literal["discussion", "progress", "decision", "blocker", "review"] = "discussion"
    recipient_id: UUID | None = None
    task_id: UUID | None = None


class TaskInput(Input):
    task_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=16000)
    parent_task_id: UUID | None = None
    repository_id: UUID | None = None


class TaskUpdate(Input):
    version: int = Field(ge=1)
    participant_id: UUID | None = None
    status: Literal["working", "blocked", "review", "done", "cancelled"] | None = None
    result_summary: str | None = Field(default=None, max_length=16000)

    @model_validator(mode="after")
    def exactly_one_action(self) -> "TaskUpdate":
        if (self.participant_id is None) == (self.status is None):
            raise ValueError("Choose exactly one assignment or state transition")
        return self


class StateInput(Input):
    status: Literal["active", "paused", "closed"]


class UsageWindow(Input):
    limit_id: str = Field(min_length=1, max_length=200)
    window_key: str = Field(min_length=1, max_length=100)
    used_percent: float | None = Field(default=None, ge=0, le=100)
    resets_at: AwareDatetime | None = None
    duration_seconds: int | None = Field(default=None, gt=0)


class UsageInput(Input):
    availability: Literal["available", "unavailable"]
    error_code: str | None = Field(default=None, max_length=100)
    windows: list[UsageWindow] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def valid_windows(self) -> "UsageInput":
        if (self.availability == "available") != bool(self.windows):
            raise ValueError(
                "Available observations need windows; unavailable ones cannot contain them"
            )
        if len({(w.limit_id, w.window_key) for w in self.windows}) != len(self.windows):
            raise ValueError("Duplicate quota windows")
        return self


def create_hub(
    engine: Engine, service: Rooms, bootstrap_key: str, *, recover: bool = True
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        if recover:

            def recovery() -> None:
                with engine.begin() as c:
                    room_ids: list[UUID] = list(
                        c.execute(
                            select(s.rooms.c.id)
                            .where(s.rooms.c.status.in_(["active", "draining"]))
                            .order_by(s.rooms.c.id)
                            .with_for_update()
                        )
                        .scalars()
                        .all()
                    )
                    for room_id in room_ids:
                        c.execute(
                            update(s.rooms).where(s.rooms.c.id == room_id).values(status="paused")
                        )
                        service._event(c, room_id, "room.recovery_required", {"status": "paused"})

            await asyncio.to_thread(recovery)
        yield

    app = FastAPI(title="Sahakaram room host", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "code": exc.code, "message": exc.message}, status_code=exc.status
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        return JSONResponse(
            {
                "ok": False,
                "code": "CONSTRAINT_CONFLICT",
                "message": "A relationship or uniqueness constraint rejected the operation",
            },
            status_code=409,
        )

    @app.middleware("http")
    async def reject_browser_cross_origin(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin != expected:
            return JSONResponse({"code": "ORIGIN_DENIED"}, status_code=403)
        return await call_next(request)

    def bootstrap(x_sahakaram_bootstrap: Annotated[str | None, Header()] = None) -> None:
        if not x_sahakaram_bootstrap or not secrets.compare_digest(
            x_sahakaram_bootstrap, bootstrap_key
        ):
            raise DomainError("AUTH_REQUIRED", "Local host authorization required", 401)

    def actor(
        authorization: Annotated[str | None, Header()] = None,
        x_sahakaram_room: Annotated[UUID | None, Header()] = None,
    ) -> Actor:
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("AUTH_REQUIRED", "Room authorization required", 401)
        identity = service.authenticate(authorization[7:])
        if x_sahakaram_room is not None and x_sahakaram_room != identity.room_id:
            raise DomainError("ROOM_MISMATCH", "Credential belongs to a different room", 403)
        return identity

    ActorDependency = Annotated[Actor, Depends(actor)]

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"service": "Sahakaram", "status": "alive"}

    @app.get("/readyz")
    def ready() -> JSONResponse:
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
                c.execute(select(s.rooms.c.id).limit(1))
        except SQLAlchemyError:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/api/v1/identities", dependencies=[Depends(bootstrap)])
    def register(data: IdentityInput) -> dict[str, str]:
        return service.register(**data.model_dump())

    @app.post("/api/v1/rooms", dependencies=[Depends(bootstrap)])
    def create(data: RoomInput) -> dict[str, Any]:
        return service.create(**data.model_dump())

    @app.post("/api/v1/join")
    def join(data: JoinInput) -> dict[str, Any]:
        return service.join(**data.model_dump())

    @app.get("/api/v1/room")
    def snapshot(identity: ActorDependency) -> dict[str, Any]:
        return service.snapshot(identity)

    @app.get("/api/v1/events")
    def replay(
        identity: ActorDependency, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)
    ) -> dict[str, Any]:
        return service.replay(identity, after, limit)

    @app.post("/api/v1/invitations")
    def invite(data: InviteInput, identity: ActorDependency) -> dict[str, str]:
        return service.invite(identity, **data.model_dump())

    @app.post("/api/v1/messages")
    def message(data: MessageInput, identity: ActorDependency) -> dict[str, Any]:
        return service.message(identity, **data.model_dump())

    @app.post("/api/v1/tasks")
    def task(data: TaskInput, identity: ActorDependency) -> dict[str, Any]:
        return service.create_task(identity, **data.model_dump())

    @app.patch("/api/v1/tasks/{task_id}")
    def task_update(task_id: UUID, data: TaskUpdate, identity: ActorDependency) -> dict[str, Any]:
        return service.task_update(identity, task_id, **data.model_dump())

    @app.post("/api/v1/room/state")
    def state(data: StateInput, identity: ActorDependency) -> dict[str, Any]:
        return service.room_state(identity, data.status)

    @app.post("/api/v1/participants/{participant_id}/kick")
    def kick(participant_id: UUID, identity: ActorDependency) -> dict[str, bool]:
        service.kick(identity, participant_id)
        return {"ok": True}

    @app.post("/api/v1/leave")
    def leave(identity: ActorDependency) -> dict[str, bool]:
        service.leave(identity)
        return {"ok": True}

    @app.post("/api/v1/usage")
    def usage(data: UsageInput, identity: ActorDependency) -> dict[str, str]:
        return service.record_usage(
            identity, data.availability, [w.model_dump() for w in data.windows], data.error_code
        )

    @app.websocket("/api/v1/events/ws")
    async def websocket(socket: WebSocket, after: int = 0) -> None:
        authorization = socket.headers.get("authorization", "")
        origin = socket.headers.get("origin")
        scheme = "https" if socket.url.scheme == "wss" else "http"
        expected = f"{scheme}://{socket.headers.get('host', '')}"
        if not authorization.startswith("Bearer ") or (origin and origin != expected) or after < 0:
            await socket.close(code=1008)
            return
        try:
            await asyncio.to_thread(service.authenticate, authorization[7:])
        except DomainError:
            await socket.close(code=1008)
            return
        await socket.accept()
        cursor = after
        try:
            while True:
                identity = await asyncio.to_thread(service.authenticate, authorization[7:])
                batch = await asyncio.to_thread(service.replay, identity, cursor, 50)
                if batch["events"]:
                    await socket.send_json(jsonable_encoder(batch))
                    cursor = batch["next_cursor"]
                # Receive a heartbeat or detect disconnect; no unbounded background queue.
                try:
                    await asyncio.wait_for(socket.receive_text(), timeout=0.25)
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            pass
        except DomainError:
            await socket.close(code=1008)

    return app
