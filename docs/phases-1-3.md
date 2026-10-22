> Historical v0.3 implementation record. Current V1 uses SQLite, a standalone backend, and Dev Tunnels. See backend.md and friend-demo.md.

# Phases 1–3 implementation record

Implemented version: 0.3.0. Python application code only. Phase 0 files and its
historical record were retained. Its edited MCP instructions were preserved as
text while fixing an accidental tuple that prevented the protocol handshake.

## Components and request flow

```text
Human terminal: setup / unlock / login / lock
                 |
MCP stdio tools -> local control HTTP (127.0.0.1:8787)
                 | local registry + isolated account CODEX_HOME
                 | official Codex Python SDK -> owned Codex app-server process
                 |
MCP room tools -> room HTTP/WebSocket host (127.0.0.1:8788)
                 | Rooms transactional domain service
                 | SQLAlchemy Core + Psycopg -> PostgreSQL
                 | Alembic revision 0001, explicit upgrade command
```

The local service owns account execution and provider credentials. The room host
owns collaboration records and permissions. Room messages never grant execution
authority on another machine. The MCP layer calls these services rather than
duplicating their mutable state. These are local processes in this increment;
remote networking and the human dashboard are later work.

## Increment 1: local control and Codex

`local/registry.py` stores the profile, account registrations, and owned session
workspace mappings in an atomically replaced, file-locked local JSON registry.
The profile passphrase has an Argon2 verifier. Unlock creates an in-memory bearer
session with a one-hour TTL; restarting the service or locking revokes it.
The CLI stores its unlock token locally for authorized MCP calls. It never prints
that token or asks an LLM to handle the passphrase.

Each account has a new isolated home below the selected state directory; no
global Codex authentication/configuration is copied. The adapter explicitly
selects file-based credential storage and blanks inherited provider API-key
environment variables. Account labels are human choices, not proof of independent
provider accounts or quotas. Provider authentication remains a human browser
action. Never share the account home with another running Codex process.

The coordinator serializes root operations for each account; separate accounts
can run concurrently. Sessions require an explicit existing absolute workspace.
Creation defaults to read-only, workspace-write is explicit, and resumed sessions
are read-only. Provider approval escalation is denied. Session ownership is checked
before resume, execution, or interruption. Interruption bypasses the account turn
queue. Lock closes runtimes, invalidates queued operations across a generation
change, and requires another unlock. A cancelled turn closes its owned runtime
before its account execution lock is released.

Local HTTP endpoints include profile/unlock/lock, account creation/listing/login,
usage, owned sessions, resume, turn, and interrupt. Browser requests require the
loopback Host and same Origin; state-changing browser calls require
`X-Sahakaram-CSRF: 1`. Local API documentation is available at `/docs`.

MCP exposes `list_accounts`, `add_account`, `get_usage`, `list_sessions`,
`start_session`, `resume_session`, `run_turn`, and `interrupt_turn`. These use
an existing terminal unlock. Provider login is `sahakaram login ACCOUNT_ID`.

### SDK compatibility and measured scope

The optional extra pins `openai-codex==0.163.0`. The official high-level Python API
is used for account login, sessions, turns, and interruption. Its public wrapper
does not expose rate-limit reads in this pinned version. One isolated compatibility
method uses the SDK's already-owned transport for `account/rateLimits/read`,
guarded by version, signature, and generated response-model checks. It fails with
an explicit unavailable reason on drift rather than guessing allowance. It does
not launch a second app-server or scrape terminal screens.

Limits retain distinct bucket IDs, primary/secondary windows, used percentages,
reset Unix seconds, and window duration minutes. Unauthenticated, unsupported,
unrecognized, and unavailable responses are distinguished from available windows.
Provider reports remain observations; no quota savings or throughput increase has
been measured.

During implementation the optional SDK's bundled CLI download stalled. Base
dependencies and the Python wrapper were installed with:

```powershell
uv sync --locked --extra codex --no-install-package openai-codex-cli-bin
```

An explicit existing Codex executable was used only for smoke validation:
`C:/Users/satya/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe`, version 0.149.0.
The real isolated handshake and account/usage request completed; the profile was
unauthenticated and correctly returned `authentication-required`. No provider
login or real inference was run. Local artifacts are `.state/codex-smoke.json`
and `.state/codex-limits-smoke.json`. Normal installations should use the full
extra, or explicitly set `SAHAKARAM_CODEX_BIN` to an available compatible runtime.
Use `uv run --locked --extra codex ...` consistently, including MCP configuration,
when managing Codex sessions. For the current wrapper-only installation, run the
existing environment with `.venv/Scripts/python.exe -m sahakaram ...` or use
`uv run --no-sync ...` to avoid retrying the bundled binary download.

References checked during implementation: [official SDK documentation](https://developers.openai.com/codex/sdk)
and [app-server protocol](https://developers.openai.com/codex/app-server). Runtime
support beyond the tested handshake is not established by those documents alone.

## Increment 2: PostgreSQL persistence

There are fourteen domain tables: users, agent_accounts, projects, repositories,
project_repositories, rooms, room_memberships, participants, tasks, messages,
usage_snapshots, usage_windows, invitations, and room_events. Alembic adds its own
revision tracking table. `users` contains exactly `id` and `displayname`.
There is no devices table or access_credentials table.

Projects have multiple repositories through a junction table. Rooms can have no
project. Human membership/admin and agent participation/lead are separate facts.
Provider secrets remain local. Signed room credentials are issued by the host;
current database status and token_version are checked on every authorized call.

UUIDs identify domain objects; event cursors are per-room bigint sequences.
Composite foreign keys reject cross-room references. Unique/partial indexes bound
admin and lead roles. Task versions reject stale writes. Room row locks serialize
admission, and an account row lock serializes task claims across rooms. Account
usage observations and their independent windows are normalized child records.

Mutation and its event commit together. Replay reads committed rows with a bounded
page and next_cursor; a room snapshot is repeatable-read. Snapshot tasks are
bounded to 100 rows. Event replay is the authoritative history; snapshots are not
a full task-history export. Queries use a bounded connection pool. No load or
latency claim is made.

`sahakaram db-upgrade` operates only on the explicitly selected PostgreSQL URL.
There is no automatic fallback database, automatic schema reset, or destructive
downgrade. Revision 0001 imports a frozen schema snapshot. Future schema changes
must add revisions rather than modifying that snapshot. Initial migration has
been repeated successfully against an existing migrated test schema.

## Increment 3: local room collaboration

The host's terminal unlock authorizes MCP bootstrap on loopback. The host creates
a general room and receives a human admin plus agent lead internally. Default
capacity is three agent seats, including the lead. Human-only dashboard invites
do not consume agent seats. Invitations are random, stored as hashes, one use by
default, and expire after 30 minutes. Redemption and seat allocation are one
transaction; losing a race does not consume an invitation.

Room bearer credentials last 15 minutes. The MCP bridge stores them locally and
removes them from create/join tool output. A configured bearer override is also
supported. Refresh, re-enrollment, role transfer, and credential recovery are not
implemented in this local milestone; expired credentials cannot be silently
replaced. The dashboard/tunnel authentication phase must address long sessions.
Revocation through kick/leave invalidates credentials immediately, independently
of expiry. Windows file protection relies on the user's state-directory ACL;
POSIX-style chmod is not a substitute for Windows ACL management.

Same-user linking is a room-membership operation through the join HTTP API:
the administrator may set a `link_phrase` when creating the room; another account
joins with an invitation, `existing_user_id`, and that phrase. Only a verifier is
stored. It does not authenticate a provider account or combine quotas. This is
separate from the local terminal unlock phrase. MCP intentionally does not solicit
either phrase; a human supplies it through the local/API flow until the dashboard
exists.

Agents exchange bounded messages and create/assign/claim tasks. Message/task
creation UUIDs support identical retries. Task updates require an expected version.
Assignments require the lead or human administrator; working/review updates require
the assignee; completion requires lead/admin review. One account can hold at most
one working task across rooms. Assignments are records, not automatic inference.

An administrator may pause/resume/close or kick a teammate. Lead/admin departure
requires closing the room first; role transfer is not provided yet. Leaving blocks
unfinished assigned work. Human departure also revokes owned participants and
releases their seats. Startup pauses previously active/draining rooms and records
recovery events, requiring explicit human resumption.

HTTP and WebSocket event replay both check room authorization. The bridge binds
each request to the requested room ID. WebSocket connections recheck expiry and
revocation during polling. PostgreSQL replay is polled at a bounded interval;
NOTIFY and remote transport are not required for this local implementation.

`share_usage` converts a local observation's durations to seconds and resets to
timezone-aware timestamps before storing it for the current participant's account.
`get_room` includes latest observations and their timestamps; unavailable remains
unavailable. These are companion self-reports, not server-verified provider identity.
No polling scheduler, stale-quota classifier, automatic delegation, communication
cadence enforcement, or quota-triggered draining is implemented in this phase.

Project-backed admission fails closed until GitHub collaborator verification is
implemented. This milestone does not push code or impersonate a GitHub owner.

## Validation and reproduction

The tests cover the actual stdio protocol, local unlock/lock and owned execution,
isolation, cancellation/serialization, quota compatibility, transactional room
behavior, stale task versions, competing admission, cross-room constraints,
revocation, restart recovery, and WebSocket replay. Separate MCP subprocesses
traverse an actual HTTP hub into PostgreSQL in `test_mcp_rooms.py`; agents are
scripted. `test_local_mcp.py` traverses MCP into the local HTTP service with a
fake provider. Quota conversion is checked through MCP into real PostgreSQL.

```powershell
$env:SAHAKARAM_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55439/sahakaram_test'
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy
uv build
```

The PostgreSQL tests create/drop only a unique random schema in the explicitly
selected test database. They skip when its variable is unset. The native disposable
PostgreSQL runtime is under `.state/phase23-postgres`; it is not a system service.
Its local provenance, checksum, startup and shutdown commands are recorded in
`.state/phase23-postgres/PROVENANCE.txt`. It uses loopback trust authentication for
disposable testing only. No existing application database was altered.

The wheel and source archive were built and inspected: migration code and the
frozen schema are included; local state, provider credentials, the PostgreSQL
runtime, and the virtual environment are excluded. This validation establishes
local scripted collaboration and an unauthenticated Codex handshake. It does not
establish live model collaboration, cross-device tunnels, GitHub execution,
production authentication, or measured scalability.

## Next boundary

Phase 4 should add the Python dashboard, human approval flows, secure credential
renewal/recovery, and authenticated remote transport. Phase 5 should connect
supervised task execution, bounded discussion and usage-aware draining with
failure/recovery tests. Phase 6 should enforce GitHub repository permissions and
the intended contribution workflow. Keep these separate from the working local
slice rather than treating future schema fields as completed behavior.
