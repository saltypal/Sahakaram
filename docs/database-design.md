# V1 SQLite design

The backend alone owns the local SQLite file. Agents, plugins, dashboards, and
other devices call its HTTP API; they never open or synchronize the database file.
Do not put an active database on a network share or cloud-synced folder.

## Setup and preservation

By default, the backend creates `sahakaram.sqlite3` under its private state directory
and applies Alembic migrations at startup. An optional `SAHAKARAM_DATABASE_URL`
may select another local `sqlite+pysqlite:///...` file. PostgreSQL URLs are rejected
with a clear error. Existing PostgreSQL databases, credentials and room history
are untouched; they are not automatically converted or imported.

The SQLite revision is `sqlite_0001`, using frozen `storage/sqlite_v1.py` metadata.
The historical PostgreSQL snapshot/revision remains preserved for reference, not
used at runtime. Future SQLite changes require new revisions. Re-running upgrades
preserves data; no destructive downgrade is provided.

## Normalization and integrity

The fourteen domain tables remain: users, agent_accounts, projects, repositories,
project_repositories, rooms, room_memberships, participants, tasks, messages,
usage_snapshots, usage_windows, invitations, room_events.

- `users` has exactly `id` and `displayname`.
- One project can reference multiple repositories through a junction table.
- General rooms may have no project. GitHub-backed admission remains gated until
  collaborator verification is implemented.
- Human memberships and agent participants have separate roles and lifecycles.
- Composite foreign keys reject cross-room task/message/participant references.
- Partial unique indexes restrict active admins and reserved leads per room.
- Independent account usage windows remain separate rows, with limit and window IDs.
- There is no devices table or access_credentials table. Provider credentials stay local.

UUIDs use SQLAlchemy's portable UUID representation; event payloads use JSON.
Timestamp adapters write UTC and restore timezone-aware values on read. Usage
observation times are explicitly recorded with microsecond precision so consecutive
observations do not depend on SQLite's second-resolution CURRENT_TIMESTAMP.

## Transactions and concurrency

Every connection enables foreign keys, WAL, full synchronization and a bounded
10-second busy timeout. Runtime mutations acquire `BEGIN IMMEDIATE` before reading
seat counts, invitation uses or task versions. This serializes competing writers
across rooms and processes using the same local file, preserving atomic admission,
single working-task ownership, event ordering and version checks.

Read transactions use explicit `BEGIN`, giving coherent snapshots while WAL allows
a writer to commit. Mutation and its event commit in the same transaction. Snapshot
pages and replay batches remain bounded. This is a small-room, single-backend V1;
there is no distributed database, sharding or throughput claim.

Keep the database file, WAL and shared-memory files private. Do not copy only the
main file while it is active; use SQLite's backup API or stop the backend cleanly
before taking a file backup. File-level storage is an operational simplification,
not removal of transaction or integrity requirements.

See [the backend contract](backend.md) and [historical PostgreSQL design](history/postgresql-design.md).
