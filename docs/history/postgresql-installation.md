> Historical v0.3 design. V1 uses SQLite and Dev Tunnels; see ../../README.md.

# Sahakaram

Python-only application for supervised collaboration between coding agents.

## Implemented: Phases 1–3

Sahakaram now has an authenticated local control service, isolated Codex account
homes and owned sessions, PostgreSQL persistence, and local rooms with MCP tools.
Three coding clients can join a room, exchange messages, assign and claim tasks,
publish reviews, and replay events after reconnecting. A room can work without a
project or repository. A human administrator can pause, resume, or close a room
and remove teammates.

The black/yellow dashboard and remote tunnel are Phase 4. Automatic task execution,
communication scheduling, and quota-triggered draining are Phase 5. GitHub access
verification and repository workflows are Phase 6. These later capabilities are
not enabled by database columns reserved for them.

## Install and run

Install [uv](https://docs.astral.sh/uv/), then run these commands from this checkout:

```powershell
uv sync --locked
uv run --locked sahakaram version
uv run --locked sahakaram doctor
```

The base installation supports MCP and rooms without the optional Codex runtime.
For managed Codex sessions, install `uv sync --locked --extra codex`. This downloads
the pinned official Python SDK and its runtime. No Node.js or TypeScript is needed.

Create the local profile and unlock it interactively:

```powershell
uv run --locked sahakaram setup --displayname Alice
uv run --locked sahakaram unlock
```

Both commands start the loopback control service when necessary. The passphrase
is entered in the terminal, never in a model prompt or command argument. Unlock
lasts one hour; restarting the control service invalidates unlock sessions.
`uv run --locked sahakaram lock` revokes them and closes owned Codex runtimes.
`sahakaram dashboard` currently opens the local API documentation, not the planned UI.

For rooms, create/select a PostgreSQL database, then explicitly migrate and host it:

```powershell
$env:SAHAKARAM_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/sahakaram'
uv run --locked sahakaram db-upgrade
uv run --locked sahakaram hub
```

Use a real database credential in your environment; do not commit it. The hub binds
only to `127.0.0.1:8788`. PostgreSQL is unnecessary for standalone account/session
management. Migrations are explicit and forward-only; starting MCP never migrates
or installs a database. One hub owns a database in this local phase.

## Connect a coding client

Configure a stdio MCP server using the following command and arguments. Replace
the project path on another machine. The explicit project argument allows the
client to launch the server from any working directory.

```toml
[mcp_servers.sahakaram]
command = "uv"
args = ["run", "--locked", "--project", "D:/Bunker/BaseCamp/MCP/Sahakaram", "sahakaram", "mcp"]

[mcp_servers.sahakaram.env]
SAHAKARAM_STATE_DIR = "D:/Bunker/BaseCamp/MCP/Sahakaram/.state"
SAHAKARAM_HUB_URL = "http://127.0.0.1:8788"
```

This is a configuration example, not an installer: Sahakaram does not edit the
running client's configuration. If `uv` is not on the client's PATH, supply its
absolute executable path. Reconnect/restart the client after adding the server,
then ask it to call Sahakaram's `get_status` tool. MCP waits on stdin/stdout rather
than accepting terminal chat; application logs go to stderr.

After local unlock, ask the host agent to `create_room`, then `create_invites`
twice. Each teammate uses a separate state directory and calls `join_room` with
one invitation secret. All three clients point at the same loopback hub for this
phase. Each default invitation admits one account and expires after 30 minutes.
Room credentials stay in the client's local state and are omitted from tool
results. Separate clients must use separate state directories to keep identities
separate. Room bearer credentials expire after 15 minutes; persistent login,
refresh, and recovery controls are not implemented yet.

Use `send_message`/`receive_messages`, `create_task`/`assign_task`/`update_task`,
and `get_room`. Retain event cursors and reuse message/task UUIDs only for identical
creation retries. Use the returned task version for each update. Claiming work
records responsibility; it does not automatically start inference.

For managed Codex accounts, call `add_account` and `list_accounts`, then run
`uv run --locked --extra codex sahakaram login ACCOUNT_ID --browser` yourself.
Use `get_usage`, `start_session`, `resume_session`, `run_turn`, and `interrupt_turn`
after provider login. Sessions default to read-only; workspace writes require
explicit selection. Provider approval escalation is denied. `run_turn` consumes
provider allowance. `share_usage` explicitly publishes a local observation to the
current room; `get_room` includes the latest observations and timestamps. Reports
are self-reported and do not establish distinct provider quota pools.

## Configuration

Environment variables are optional. No `.env` file is loaded automatically.

| Variable | Default | Allowed value |
| --- | --- | --- |
| `SAHAKARAM_INSTANCE_NAME` | `Sahakaram` | 1–64 characters, including a non-whitespace character |
| `SAHAKARAM_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SAHAKARAM_STATE_DIR` | `.state` | Local profile/runtime directory; use an absolute path in client config |
| `SAHAKARAM_CONTROL_PORT` | `8787` | Local control port; pass matching `--port` when starting manually |
| `SAHAKARAM_DATABASE_URL` | unset | Explicit PostgreSQL SQLAlchemy/psycopg URL |
| `SAHAKARAM_HUB_URL` | `http://127.0.0.1:8788` | Local room host |
| `SAHAKARAM_ROOM_TOKEN` | unset | Optional bearer override for a pre-enrolled client; do not paste into chat |
| `SAHAKARAM_CODEX_BIN` | SDK runtime | Optional explicit existing Codex executable |
| `SAHAKARAM_AUTO_START_CONTROL` | `true` | Disable for independent protocol tests |

## Validate

```powershell
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked mypy
```

Set `SAHAKARAM_TEST_DATABASE_URL` to an explicitly disposable PostgreSQL database
to include persistence and room integration tests. Without it, those tests skip.
Each test creates and drops only its own random schema. The full integration test
starts an actual hub subprocess and three actual MCP stdio clients; agent messages
are scripted, with no model inference. Fake Codex adapters exercise execution and
failure paths without consuming allowance.

See [the implementation record](phases-1-3.md) for architecture, test evidence,
the local PostgreSQL test runtime, SDK compatibility, and remaining limitations.
[Database design](database-design.md) distinguishes current constraints from
future workflows. [Phase 0](phase-0.md) remains the historical foundation record.
