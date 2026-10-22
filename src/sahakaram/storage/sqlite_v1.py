"""Frozen SQLite V1 schema; add a migration instead of editing this snapshot."""

from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import JSON as JSONB
from sqlalchemy import Uuid as UUID

from sahakaram.storage.types import UTCDateTime

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def identity() -> sa.Column[Any]:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4)


def timestamp(name: str = "created_at") -> sa.Column[Any]:
    return sa.Column(name, UTCDateTime(), nullable=False, server_default=sa.func.now())


users = sa.Table(
    "users",
    metadata,
    identity(),
    sa.Column("displayname", sa.String(100), nullable=False),
    sa.CheckConstraint("length(trim(displayname)) > 0", name="user_name_nonempty"),
)
accounts = sa.Table(
    "agent_accounts",
    metadata,
    identity(),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("provider", sa.String(40), nullable=False),
    sa.Column("label", sa.String(100), nullable=False),
    sa.Column("provider_account_ref", sa.String(200)),
    sa.UniqueConstraint("provider", "provider_account_ref"),
)
projects = sa.Table(
    "projects",
    metadata,
    identity(),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("description", sa.Text),
)
repositories = sa.Table(
    "repositories",
    metadata,
    identity(),
    sa.Column("github_repository_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("owner", sa.String(200), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
)
project_repositories = sa.Table(
    "project_repositories",
    metadata,
    sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), primary_key=True),
    sa.Column("repository_id", UUID, sa.ForeignKey("repositories.id"), primary_key=True),
)
rooms = sa.Table(
    "rooms",
    metadata,
    identity(),
    sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), index=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    sa.Column("max_participants", sa.Integer, nullable=False, server_default="3"),
    sa.Column("instructions", sa.Text, nullable=False, server_default=""),
    sa.Column("communication_interval_seconds", sa.Integer, nullable=False, server_default="300"),
    sa.Column("usage_limit_policy", sa.String(20), nullable=False, server_default="drain"),
    sa.Column("policy_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("last_event_sequence", sa.BigInteger, nullable=False, server_default="0"),
    sa.CheckConstraint("max_participants > 0", name="room_capacity_positive"),
    sa.CheckConstraint("communication_interval_seconds > 0", name="room_interval_positive"),
    sa.CheckConstraint("status IN ('active','draining','paused','closed')", name="room_status"),
    sa.CheckConstraint("usage_limit_policy = 'drain'", name="room_limit_policy"),
)
memberships = sa.Table(
    "room_memberships",
    metadata,
    identity(),
    sa.Column("room_id", UUID, sa.ForeignKey("rooms.id"), nullable=False),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("role", sa.String(20), nullable=False, server_default="member"),
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    sa.Column("token_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("link_secret_hash", sa.Text),
    sa.UniqueConstraint("room_id", "user_id"),
    sa.UniqueConstraint("room_id", "id"),
    sa.CheckConstraint("role IN ('admin','member')", name="membership_role"),
    sa.CheckConstraint("status IN ('active','left','revoked')", name="membership_status"),
)
participants = sa.Table(
    "participants",
    metadata,
    identity(),
    sa.Column("room_id", UUID, sa.ForeignKey("rooms.id"), nullable=False),
    sa.Column("account_id", UUID, sa.ForeignKey("agent_accounts.id"), nullable=False, index=True),
    sa.Column("github_user_id", sa.BigInteger),
    sa.Column("role", sa.String(20), nullable=False, server_default="teammate"),
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    sa.Column("token_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_seen_at", UTCDateTime()),
    sa.UniqueConstraint("room_id", "account_id"),
    sa.UniqueConstraint("room_id", "id"),
    sa.CheckConstraint("role IN ('lead','teammate')", name="participant_role"),
    sa.CheckConstraint(
        "status IN ('pending_access','active','left','kicked')", name="participant_status"
    ),
)
sa.Index(
    "one_active_admin",
    memberships.c.room_id,
    unique=True,
    sqlite_where=sa.and_(memberships.c.role == "admin", memberships.c.status == "active"),
)
sa.Index(
    "one_reserved_lead",
    participants.c.room_id,
    unique=True,
    sqlite_where=sa.and_(
        participants.c.role == "lead", participants.c.status.in_(["active", "pending_access"])
    ),
)
tasks = sa.Table(
    "tasks",
    metadata,
    identity(),
    sa.Column("room_id", UUID, sa.ForeignKey("rooms.id"), nullable=False),
    sa.Column("repository_id", UUID, sa.ForeignKey("repositories.id")),
    sa.Column("parent_task_id", UUID),
    sa.Column("assigned_participant_id", UUID),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("result_summary", sa.Text),
    sa.Column("base_commit_sha", sa.String(64)),
    sa.Column("work_branch", sa.Text),
    sa.Column("result_commit_sha", sa.String(64)),
    sa.Column("pull_request_number", sa.Integer),
    timestamp("updated_at"),
    sa.UniqueConstraint("room_id", "id"),
    sa.ForeignKeyConstraint(["room_id", "parent_task_id"], ["tasks.room_id", "tasks.id"]),
    sa.ForeignKeyConstraint(
        ["room_id", "assigned_participant_id"], ["participants.room_id", "participants.id"]
    ),
    sa.CheckConstraint(
        "status IN ('queued','assigned','working','blocked','review','done','cancelled')",
        name="task_status",
    ),
    sa.CheckConstraint(
        "status NOT IN ('assigned','working','review','done') "
        "OR assigned_participant_id IS NOT NULL",
        name="task_requires_assignee",
    ),
    sa.CheckConstraint("version > 0", name="task_version_positive"),
)
messages = sa.Table(
    "messages",
    metadata,
    identity(),
    sa.Column("room_id", UUID, sa.ForeignKey("rooms.id"), nullable=False),
    sa.Column("sender_membership_id", UUID),
    sa.Column("sender_participant_id", UUID),
    sa.Column("recipient_participant_id", UUID),
    sa.Column("task_id", UUID),
    sa.Column("kind", sa.String(20), nullable=False, server_default="discussion"),
    sa.Column("content", sa.Text, nullable=False),
    timestamp(),
    sa.CheckConstraint(
        "(sender_membership_id IS NOT NULL) <> (sender_participant_id IS NOT NULL)",
        name="one_message_sender",
    ),
    sa.CheckConstraint("length(content) BETWEEN 1 AND 2000", name="message_length"),
    sa.CheckConstraint(
        "kind IN ('discussion','progress','decision','blocker','review')", name="message_kind"
    ),
    sa.ForeignKeyConstraint(
        ["room_id", "sender_membership_id"], ["room_memberships.room_id", "room_memberships.id"]
    ),
    sa.ForeignKeyConstraint(
        ["room_id", "sender_participant_id"], ["participants.room_id", "participants.id"]
    ),
    sa.ForeignKeyConstraint(
        ["room_id", "recipient_participant_id"], ["participants.room_id", "participants.id"]
    ),
    sa.ForeignKeyConstraint(["room_id", "task_id"], ["tasks.room_id", "tasks.id"]),
)
usage_snapshots = sa.Table(
    "usage_snapshots",
    metadata,
    identity(),
    sa.Column("account_id", UUID, sa.ForeignKey("agent_accounts.id"), nullable=False),
    timestamp("observed_at"),
    sa.Column("source", sa.String(100), nullable=False),
    sa.Column("availability", sa.String(20), nullable=False),
    sa.Column("error_code", sa.String(100)),
    sa.CheckConstraint("availability IN ('available','unavailable')", name="usage_availability"),
)
usage_windows = sa.Table(
    "usage_windows",
    metadata,
    sa.Column(
        "snapshot_id",
        UUID,
        sa.ForeignKey("usage_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("limit_id", sa.String(200), primary_key=True),
    sa.Column("window_key", sa.String(100), primary_key=True),
    sa.Column("used_percent", sa.Numeric(5, 2)),
    sa.Column("resets_at", UTCDateTime()),
    sa.Column("duration_seconds", sa.Integer),
    sa.CheckConstraint("used_percent BETWEEN 0 AND 100", name="usage_percentage"),
    sa.CheckConstraint("duration_seconds > 0", name="usage_duration"),
)
invitations = sa.Table(
    "invitations",
    metadata,
    identity(),
    sa.Column("room_id", UUID, sa.ForeignKey("rooms.id"), nullable=False, index=True),
    sa.Column("kind", sa.String(20), nullable=False),
    sa.Column("target_github_user_id", sa.BigInteger),
    sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
    sa.Column("expires_at", UTCDateTime(), nullable=False),
    sa.Column("max_uses", sa.Integer, nullable=False, server_default="1"),
    sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("revoked_at", UTCDateTime()),
    sa.CheckConstraint("kind IN ('dashboard','agent')", name="invitation_kind"),
    sa.CheckConstraint("max_uses > 0 AND use_count BETWEEN 0 AND max_uses", name="invitation_uses"),
)
events = sa.Table(
    "room_events",
    metadata,
    sa.Column("room_id", UUID, sa.ForeignKey("rooms.id"), primary_key=True),
    sa.Column("sequence", sa.BigInteger, primary_key=True),
    sa.Column("kind", sa.String(100), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    timestamp(),
)
sa.Index("message_timeline", messages.c.room_id, messages.c.created_at, messages.c.id)
sa.Index("task_queue", tasks.c.room_id, tasks.c.status, tasks.c.updated_at, tasks.c.id)
sa.Index("account_tasks", tasks.c.assigned_participant_id, tasks.c.status)
sa.Index("account_usage", usage_snapshots.c.account_id, usage_snapshots.c.observed_at.desc())
sa.Index("project_repository_reverse", project_repositories.c.repository_id)
