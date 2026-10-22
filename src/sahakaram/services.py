"""Transactional room domain. No transport handlers or LLM execution live here."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID, uuid4

import jwt
import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import Connection, Engine

from sahakaram.security import Actor, Tokens
from sahakaram.storage import schema as s
from sahakaram.storage.database import write_transaction


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def fail(code: str, message: str, status: int = 409) -> NoReturn:
    raise DomainError(code, message, status)


class Rooms:
    def __init__(self, engine: Engine, tokens: Tokens) -> None:
        self.engine, self.tokens = engine, tokens
        self.hasher = PasswordHasher()

    def _actor(self, c: Connection, actor: Actor) -> dict[str, Any]:
        table = s.memberships if actor.kind == "human" else s.participants
        row = (
            c.execute(
                sa.select(table).where(table.c.id == actor.id, table.c.room_id == actor.room_id)
            )
            .mappings()
            .first()
        )
        if not row or row["status"] != "active" or row["token_version"] != actor.version:
            fail("AUTH_REQUIRED", "Room credential is no longer active", 401)
        if actor.kind == "agent":
            owner: UUID = c.execute(
                sa.select(s.accounts.c.user_id).where(s.accounts.c.id == row["account_id"])
            ).scalar_one()
            member = c.execute(
                sa.select(s.memberships.c.id).where(
                    s.memberships.c.room_id == actor.room_id,
                    s.memberships.c.user_id == owner,
                    s.memberships.c.status == "active",
                )
            ).first()
            if not member:
                fail("AUTH_REQUIRED", "Account owner is not an active room member", 401)
        return dict(row)

    def authenticate(self, token: str) -> Actor:
        try:
            actor = self.tokens.read(token)
        except (ValueError, jwt.InvalidTokenError):
            fail("AUTH_REQUIRED", "Invalid or expired room credential", 401)
        with self.engine.connect() as c:
            self._actor(c, actor)
        return actor

    def _room(self, c: Connection, room_id: UUID, *, active: bool = False) -> dict[str, Any]:
        row = c.execute(sa.select(s.rooms).where(s.rooms.c.id == room_id)).mappings().first()
        if not row:
            fail("NOT_FOUND", "Room not found", 404)
        if active and row["status"] != "active":
            fail("ROOM_NOT_ACTIVE", "Room does not accept new collaboration")
        return dict(row)

    def _authorize(self, row: dict[str, Any], actor: Actor, *, admin: bool = False) -> None:
        allowed = (actor.kind == "human" and row["role"] == "admin") or (
            not admin and actor.kind == "agent" and row["role"] == "lead"
        )
        if not allowed:
            fail(
                "FORBIDDEN",
                "This operation requires the room administrator"
                if admin
                else "This operation requires the lead or administrator",
                403,
            )

    def _event(self, c: Connection, room_id: UUID, kind: str, payload: dict[str, Any]) -> int:
        sequence: int = c.execute(
            sa.update(s.rooms)
            .where(s.rooms.c.id == room_id)
            .values(last_event_sequence=s.rooms.c.last_event_sequence + 1)
            .returning(s.rooms.c.last_event_sequence)
        ).scalar_one()
        c.execute(
            sa.insert(s.events).values(
                room_id=room_id, sequence=sequence, kind=kind, payload=payload
            )
        )
        return int(sequence)

    def register(self, displayname: str, label: str, user_id: UUID | None = None) -> dict[str, str]:
        user_id, account_id = user_id or uuid4(), uuid4()
        with write_transaction(self.engine) as c:
            if not c.execute(sa.select(s.users.c.id).where(s.users.c.id == user_id)).first():
                c.execute(sa.insert(s.users).values(id=user_id, displayname=displayname))
            c.execute(
                sa.insert(s.accounts).values(
                    id=account_id, user_id=user_id, provider="codex", label=label
                )
            )
        return {"user_id": str(user_id), "account_id": str(account_id)}

    def create(
        self,
        user_id: UUID,
        account_id: UUID,
        name: str,
        instructions: str = "",
        max_participants: int = 3,
        link_phrase: str | None = None,
    ) -> dict[str, Any]:
        room_id, member_id, participant_id = uuid4(), uuid4(), uuid4()
        link_hash = self.hasher.hash(link_phrase) if link_phrase else None
        with write_transaction(self.engine) as c:
            account = (
                c.execute(sa.select(s.accounts).where(s.accounts.c.id == account_id))
                .mappings()
                .first()
            )
            if not account or account["user_id"] != user_id:
                fail("FORBIDDEN", "Account does not belong to the selected user", 403)
            c.execute(
                sa.insert(s.rooms).values(
                    id=room_id,
                    name=name,
                    instructions=instructions,
                    max_participants=max_participants,
                )
            )
            c.execute(
                sa.insert(s.memberships).values(
                    id=member_id,
                    room_id=room_id,
                    user_id=user_id,
                    role="admin",
                    link_secret_hash=link_hash,
                )
            )
            c.execute(
                sa.insert(s.participants).values(
                    id=participant_id, room_id=room_id, account_id=account_id, role="lead"
                )
            )
            self._event(c, room_id, "room.created", {"name": name})
        return {
            "room_id": str(room_id),
            "participant_id": str(participant_id),
            "admin_token": self.tokens.issue(
                Actor(kind="human", id=member_id, room_id=room_id, version=0)
            ),
            "participant_token": self.tokens.issue(
                Actor(kind="agent", id=participant_id, room_id=room_id, version=0)
            ),
        }

    def invite(self, actor: Actor, kind: str = "agent", ttl: int = 1800) -> dict[str, str]:
        secret, invite_id = secrets.token_urlsafe(32), uuid4()
        expiry = datetime.now(UTC) + timedelta(seconds=ttl)
        with write_transaction(self.engine) as c:
            self._room(c, actor.room_id, active=True)
            self._authorize(self._actor(c, actor), actor, admin=True)
            c.execute(
                sa.insert(s.invitations).values(
                    id=invite_id,
                    room_id=actor.room_id,
                    kind=kind,
                    token_hash=hashlib.sha256(secret.encode()).hexdigest(),
                    expires_at=expiry,
                )
            )
            self._event(
                c,
                actor.room_id,
                "invitation.created",
                {"invitation_id": str(invite_id), "kind": kind},
            )
        return {"invitation_id": str(invite_id), "secret": secret, "expires_at": expiry.isoformat()}

    def join(
        self,
        secret: str,
        displayname: str,
        label: str,
        *,
        existing_user_id: UUID | None = None,
        link_phrase: str | None = None,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with write_transaction(self.engine) as c:
            initial = (
                c.execute(sa.select(s.invitations).where(s.invitations.c.token_hash == digest))
                .mappings()
                .first()
            )
            if not initial:
                fail("INVALID_INVITE", "Invitation is invalid", 403)
            room_id = initial["room_id"]
            room = self._room(c, room_id, active=True)
            invite = (
                c.execute(sa.select(s.invitations).where(s.invitations.c.id == initial["id"]))
                .mappings()
                .one()
            )
            if (
                invite["revoked_at"]
                or invite["expires_at"] <= datetime.now(UTC)
                or invite["use_count"] >= invite["max_uses"]
            ):
                fail("INVALID_INVITE", "Invitation expired, revoked, or consumed", 403)
            if room["project_id"] is not None:
                fail(
                    "REPOSITORY_ACCESS_REQUIRED",
                    "Project enrollment requires the Phase 6 GitHub verifier",
                    403,
                )
            if invite["kind"] == "agent":
                count = c.execute(
                    sa.select(sa.func.count())
                    .select_from(s.participants)
                    .where(
                        s.participants.c.room_id == room_id,
                        s.participants.c.status.in_(["active", "pending_access"]),
                    )
                ).scalar_one()
                if count >= room["max_participants"]:
                    fail("ROOM_FULL", "No agent seats remain")
            user_id = existing_user_id or uuid4()
            member = (
                c.execute(
                    sa.select(s.memberships).where(
                        s.memberships.c.room_id == room_id, s.memberships.c.user_id == user_id
                    )
                )
                .mappings()
                .first()
            )
            if existing_user_id:
                if (
                    not member
                    or not member["link_secret_hash"]
                    or not link_phrase
                    or member["status"] != "active"
                ):
                    fail(
                        "AUTH_REQUIRED",
                        "Existing user enrollment requires a room linking phrase",
                        401,
                    )
                try:
                    self.hasher.verify(member["link_secret_hash"], link_phrase)
                except VerificationError:
                    fail("AUTH_REQUIRED", "Room linking phrase is invalid", 401)
            else:
                c.execute(sa.insert(s.users).values(id=user_id, displayname=displayname))
            member_id = member["id"] if member else uuid4()
            if not member:
                c.execute(
                    sa.insert(s.memberships).values(id=member_id, room_id=room_id, user_id=user_id)
                )
            participant_id, account_id = uuid4(), uuid4()
            if invite["kind"] == "agent":
                c.execute(
                    sa.insert(s.accounts).values(
                        id=account_id, user_id=user_id, provider="codex", label=label
                    )
                )
                c.execute(
                    sa.insert(s.participants).values(
                        id=participant_id, room_id=room_id, account_id=account_id
                    )
                )
            c.execute(
                sa.update(s.invitations)
                .where(s.invitations.c.id == invite["id"])
                .values(use_count=s.invitations.c.use_count + 1)
            )
            self._event(
                c,
                room_id,
                "member.joined",
                {
                    "user_id": str(user_id),
                    "participant_id": str(participant_id) if invite["kind"] == "agent" else None,
                },
            )
            result: dict[str, Any] = {
                "room_id": str(room_id),
                "user_id": str(user_id),
                "member_token": self.tokens.issue(
                    Actor(
                        kind="human",
                        id=member_id,
                        room_id=room_id,
                        version=member["token_version"] if member else 0,
                    )
                ),
            }
            if invite["kind"] == "agent":
                result.update(
                    account_id=str(account_id),
                    participant_id=str(participant_id),
                    participant_token=self.tokens.issue(
                        Actor(kind="agent", id=participant_id, room_id=room_id, version=0)
                    ),
                )
            return result

    def snapshot(self, actor: Actor) -> dict[str, Any]:
        with self.engine.connect() as c:
            with c.begin():
                self._actor(c, actor)
                room = (
                    c.execute(sa.select(s.rooms).where(s.rooms.c.id == actor.room_id))
                    .mappings()
                    .one()
                )
                peers = (
                    c.execute(
                        sa.select(
                            s.participants.c.id,
                            s.participants.c.account_id,
                            s.participants.c.role,
                            s.participants.c.status,
                            s.accounts.c.label,
                        )
                        .join(s.accounts, s.accounts.c.id == s.participants.c.account_id)
                        .where(s.participants.c.room_id == actor.room_id)
                    )
                    .mappings()
                    .all()
                )
                work = (
                    c.execute(
                        sa.select(s.tasks)
                        .where(s.tasks.c.room_id == actor.room_id)
                        .order_by(s.tasks.c.updated_at, s.tasks.c.id)
                        .limit(100)
                    )
                    .mappings()
                    .all()
                )
                usage = []
                for peer in peers:
                    latest = (
                        c.execute(
                            sa.select(s.usage_snapshots)
                            .where(s.usage_snapshots.c.account_id == peer["account_id"])
                            .order_by(
                                s.usage_snapshots.c.observed_at.desc(), s.usage_snapshots.c.id
                            )
                            .limit(1)
                        )
                        .mappings()
                        .first()
                    )
                    if latest is not None:
                        windows = (
                            c.execute(
                                sa.select(s.usage_windows).where(
                                    s.usage_windows.c.snapshot_id == latest["id"]
                                )
                            )
                            .mappings()
                            .all()
                        )
                        usage.append({**dict(latest), "windows": [dict(w) for w in windows]})
                return {
                    "usage": usage,
                    "room": dict(room),
                    "participants": [dict(p) for p in peers],
                    "tasks": [dict(t) for t in work],
                    "cursor": room["last_event_sequence"],
                }

    def replay(self, actor: Actor, after: int = 0, limit: int = 50) -> dict[str, Any]:
        with self.engine.connect() as c:
            self._actor(c, actor)
            watermark: int = c.execute(
                sa.select(s.rooms.c.last_event_sequence).where(s.rooms.c.id == actor.room_id)
            ).scalar_one()
            if after > watermark:
                fail("INVALID_CURSOR", "Cursor is ahead of the room", 422)
            rows = (
                c.execute(
                    sa.select(s.events)
                    .where(s.events.c.room_id == actor.room_id, s.events.c.sequence > after)
                    .order_by(s.events.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return {
                "events": [dict(e) for e in rows],
                "next_cursor": rows[-1]["sequence"] if rows else after,
            }

    def message(
        self,
        actor: Actor,
        message_id: UUID,
        content: str,
        kind: str = "discussion",
        recipient_id: UUID | None = None,
        task_id: UUID | None = None,
    ) -> dict[str, Any]:
        with write_transaction(self.engine) as c:
            self._room(c, actor.room_id, active=True)
            self._actor(c, actor)
            sender_col = (
                s.messages.c.sender_membership_id
                if actor.kind == "human"
                else s.messages.c.sender_participant_id
            )
            existing = (
                c.execute(sa.select(s.messages).where(s.messages.c.id == message_id))
                .mappings()
                .first()
            )
            if existing:
                if (
                    existing["room_id"] != actor.room_id
                    or existing[sender_col.name] != actor.id
                    or existing["content"] != content
                    or existing["kind"] != kind
                    or existing["recipient_participant_id"] != recipient_id
                    or existing["task_id"] != task_id
                ):
                    fail("ID_CONFLICT", "Message ID already has different content")
                return {"id": str(message_id), "replayed": True}
            for table, value in ((s.participants, recipient_id), (s.tasks, task_id)):
                if (
                    value
                    and not c.execute(
                        sa.select(table.c.id).where(
                            table.c.id == value, table.c.room_id == actor.room_id
                        )
                    ).first()
                ):
                    fail("CROSS_ROOM_REFERENCE", "Message reference is outside this room", 422)
            c.execute(
                sa.insert(s.messages).values(
                    id=message_id,
                    room_id=actor.room_id,
                    **{sender_col.name: actor.id},
                    recipient_participant_id=recipient_id,
                    task_id=task_id,
                    content=content,
                    kind=kind,
                )
            )
            sequence = self._event(
                c,
                actor.room_id,
                "message.created",
                {
                    "id": str(message_id),
                    "sender_id": str(actor.id),
                    "kind": kind,
                    "content": content,
                    "recipient_id": str(recipient_id) if recipient_id else None,
                    "task_id": str(task_id) if task_id else None,
                },
            )
            return {"id": str(message_id), "sequence": sequence, "replayed": False}

    def create_task(
        self,
        actor: Actor,
        task_id: UUID,
        title: str,
        description: str = "",
        parent_task_id: UUID | None = None,
        repository_id: UUID | None = None,
    ) -> dict[str, Any]:
        with write_transaction(self.engine) as c:
            room = self._room(c, actor.room_id, active=True)
            self._authorize(self._actor(c, actor), actor)
            existing = (
                c.execute(sa.select(s.tasks).where(s.tasks.c.id == task_id)).mappings().first()
            )
            if existing:
                if any(
                    existing[k] != v
                    for k, v in {
                        "room_id": actor.room_id,
                        "title": title,
                        "description": description,
                        "parent_task_id": parent_task_id,
                        "repository_id": repository_id,
                    }.items()
                ):
                    fail("ID_CONFLICT", "Task ID already has different content")
                return dict(existing)
            if (
                parent_task_id
                and not c.execute(
                    sa.select(s.tasks.c.id).where(
                        s.tasks.c.id == parent_task_id, s.tasks.c.room_id == actor.room_id
                    )
                ).first()
            ):
                fail("CROSS_ROOM_REFERENCE", "Parent task is outside this room", 422)
            if (
                repository_id
                and not c.execute(
                    sa.select(s.project_repositories).where(
                        s.project_repositories.c.project_id == room["project_id"],
                        s.project_repositories.c.repository_id == repository_id,
                    )
                ).first()
            ):
                fail(
                    "REPOSITORY_ACCESS_DENIED",
                    "Repository is not associated with this room project",
                    403,
                )
            row = (
                c.execute(
                    sa.insert(s.tasks)
                    .values(
                        id=task_id,
                        room_id=actor.room_id,
                        title=title,
                        description=description,
                        parent_task_id=parent_task_id,
                        repository_id=repository_id,
                    )
                    .returning(s.tasks)
                )
                .mappings()
                .one()
            )
            self._event(c, actor.room_id, "task.created", {"task_id": str(task_id), "title": title})
            return dict(row)

    def task_update(
        self,
        actor: Actor,
        task_id: UUID,
        version: int,
        *,
        participant_id: UUID | None = None,
        status: str | None = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        with write_transaction(self.engine) as c:
            # SQLite writer reservation precedes every state check, including across rooms.
            acting = self._actor(c, actor)
            if actor.kind == "agent":
                c.execute(sa.select(s.accounts.c.id).where(s.accounts.c.id == acting["account_id"]))
            room = self._room(c, actor.room_id)
            acting = self._actor(c, actor)
            row = (
                c.execute(
                    sa.select(s.tasks).where(
                        s.tasks.c.id == task_id, s.tasks.c.room_id == actor.room_id
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                fail("NOT_FOUND", "Task not found", 404)
            if row["version"] != version:
                fail("VERSION_CONFLICT", "Task has changed; fetch it before retrying")
            values: dict[str, Any] = {"version": version + 1, "updated_at": datetime.now(UTC)}
            if participant_id:
                self._authorize(acting, actor)
                if room["status"] != "active" or row["status"] not in (
                    "queued",
                    "assigned",
                    "blocked",
                ):
                    fail("INVALID_TRANSITION", "Room/task does not allow assignment")
                target = (
                    c.execute(
                        sa.select(s.participants).where(
                            s.participants.c.id == participant_id,
                            s.participants.c.room_id == actor.room_id,
                            s.participants.c.status == "active",
                        )
                    )
                    .mappings()
                    .first()
                )
                if not target:
                    fail("CROSS_ROOM_REFERENCE", "Assignee is not an active room participant", 422)
                owner: UUID = c.execute(
                    sa.select(s.accounts.c.user_id).where(s.accounts.c.id == target["account_id"])
                ).scalar_one()
                if not c.execute(
                    sa.select(s.memberships.c.id).where(
                        s.memberships.c.room_id == actor.room_id,
                        s.memberships.c.user_id == owner,
                        s.memberships.c.status == "active",
                    )
                ).first():
                    fail("FORBIDDEN", "Assignee owner is not active", 403)
                values.update(assigned_participant_id=participant_id, status="assigned")
            else:
                transitions = {
                    "queued": {"cancelled"},
                    "assigned": {"working", "blocked", "cancelled"},
                    "working": {"blocked", "review", "cancelled"},
                    "blocked": {"working", "cancelled"},
                    "review": {"done", "working", "cancelled"},
                    "done": set(),
                    "cancelled": set(),
                }
                if status not in transitions[row["status"]]:
                    fail("INVALID_TRANSITION", "Task state transition is invalid")
                if status in ("done", "cancelled"):
                    self._authorize(acting, actor)
                elif actor.kind != "agent" or row["assigned_participant_id"] != actor.id:
                    fail("FORBIDDEN", "Only the assignee can report this transition", 403)
                if status == "working":
                    if room["status"] != "active":
                        fail("ROOM_NOT_ACTIVE", "Room cannot start work")
                    busy = c.execute(
                        sa.select(s.tasks.c.id)
                        .join(
                            s.participants, s.participants.c.id == s.tasks.c.assigned_participant_id
                        )
                        .where(
                            s.participants.c.account_id == acting["account_id"],
                            s.tasks.c.status == "working",
                            s.tasks.c.id != task_id,
                        )
                    ).first()
                    if busy:
                        fail("ACCOUNT_BUSY", "Account already has working task in another session")
                values["status"] = status
                if result_summary is not None:
                    values["result_summary"] = result_summary
            updated = (
                c.execute(
                    sa.update(s.tasks)
                    .where(s.tasks.c.id == task_id)
                    .values(**values)
                    .returning(s.tasks)
                )
                .mappings()
                .one()
            )
            self._event(
                c,
                actor.room_id,
                "task.updated",
                {
                    "task_id": str(task_id),
                    "version": updated["version"],
                    "status": updated["status"],
                },
            )
            return dict(updated)

    def room_state(self, actor: Actor, status: str) -> dict[str, Any]:
        with write_transaction(self.engine) as c:
            room = self._room(c, actor.room_id)
            self._authorize(self._actor(c, actor), actor, admin=True)
            if room["status"] == "closed":
                fail("ROOM_CLOSED", "Closed rooms cannot resume")
            c.execute(sa.update(s.rooms).where(s.rooms.c.id == actor.room_id).values(status=status))
            self._event(c, actor.room_id, "room.state", {"status": status})
            return {"status": status}

    def kick(self, actor: Actor, participant_id: UUID) -> None:
        with write_transaction(self.engine) as c:
            self._room(c, actor.room_id)
            self._authorize(self._actor(c, actor), actor, admin=True)
            target = (
                c.execute(
                    sa.select(s.participants).where(
                        s.participants.c.id == participant_id,
                        s.participants.c.room_id == actor.room_id,
                    )
                )
                .mappings()
                .first()
            )
            if not target:
                fail("NOT_FOUND", "Participant not found", 404)
            if target["role"] == "lead":
                fail(
                    "LEAD_REQUIRED",
                    "Transfer leadership or close the room before removing its lead",
                )
            c.execute(
                sa.update(s.participants)
                .where(s.participants.c.id == participant_id)
                .values(status="kicked", token_version=s.participants.c.token_version + 1)
            )
            c.execute(
                sa.update(s.tasks)
                .where(
                    s.tasks.c.assigned_participant_id == participant_id,
                    s.tasks.c.status.in_(["assigned", "working", "review"]),
                )
                .values(
                    status="blocked", version=s.tasks.c.version + 1, updated_at=datetime.now(UTC)
                )
            )
            self._event(
                c, actor.room_id, "participant.kicked", {"participant_id": str(participant_id)}
            )

    def leave(self, actor: Actor) -> None:
        with write_transaction(self.engine) as c:
            room = self._room(c, actor.room_id)
            current = self._actor(c, actor)
            if current["role"] in ("admin", "lead") and room["status"] != "closed":
                fail(
                    "ROLE_TRANSFER_REQUIRED", "Close the room or transfer your role before leaving"
                )
            table = s.memberships if actor.kind == "human" else s.participants
            c.execute(
                sa.update(table)
                .where(table.c.id == actor.id)
                .values(status="left", token_version=table.c.token_version + 1)
            )
            affected = (
                [actor.id]
                if actor.kind == "agent"
                else list(
                    c.execute(
                        sa.select(s.participants.c.id)
                        .join(s.accounts, s.accounts.c.id == s.participants.c.account_id)
                        .where(
                            s.participants.c.room_id == actor.room_id,
                            s.accounts.c.user_id == current["user_id"],
                            s.participants.c.status.in_(["active", "pending_access"]),
                        )
                    ).scalars()
                )
            )
            if actor.kind == "human" and affected:
                c.execute(
                    sa.update(s.participants)
                    .where(s.participants.c.id.in_(affected))
                    .values(status="left", token_version=s.participants.c.token_version + 1)
                )
            if affected:
                c.execute(
                    sa.update(s.tasks)
                    .where(
                        s.tasks.c.assigned_participant_id.in_(affected),
                        s.tasks.c.status.in_(["assigned", "working", "review"]),
                    )
                    .values(
                        status="blocked",
                        version=s.tasks.c.version + 1,
                        updated_at=datetime.now(UTC),
                    )
                )
            self._event(
                c, actor.room_id, "member.left", {"actor_id": str(actor.id), "kind": actor.kind}
            )

    def record_usage(
        self,
        actor: Actor,
        availability: str,
        windows: list[dict[str, Any]],
        error_code: str | None = None,
    ) -> dict[str, str]:
        with write_transaction(self.engine) as c:
            self._room(c, actor.room_id)
            row = self._actor(c, actor)
            if actor.kind != "agent":
                fail("FORBIDDEN", "Usage must be reported by the participating account", 403)
            snapshot_id = uuid4()
            c.execute(
                sa.insert(s.usage_snapshots).values(
                    id=snapshot_id,
                    account_id=row["account_id"],
                    source="companion",
                    observed_at=datetime.now(UTC),
                    availability=availability,
                    error_code=error_code,
                )
            )
            if windows:
                c.execute(
                    sa.insert(s.usage_windows), [{**w, "snapshot_id": snapshot_id} for w in windows]
                )
            self._event(
                c,
                actor.room_id,
                "usage.observed",
                {
                    "snapshot_id": str(snapshot_id),
                    "account_id": str(row["account_id"]),
                    "availability": availability,
                },
            )
            return {"snapshot_id": str(snapshot_id)}
