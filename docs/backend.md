# Standalone backend contract

The backend is an HTTP application and owns all collaboration state and policy.
It does not import the MCP server, MCP bridge, local Codex control API, or Codex SDK.
Installing the base package does not install MCP or the Codex SDK; those are extras.

```text
MCP plugin ---------+
                    +--> /api/v1 HTTP backend --> local SQLite
Future dashboard ---+

Optional local Codex control --> isolated provider runtimes
Dev Tunnels / port forwarding --> transport to the backend, outside domain logic
```

## Run without a plugin

```sh
git clone https://github.com/saltypal/Sahakaram.git
cd Sahakaram
uv sync --locked
uv run --locked sahakaram-backend --port 8788 --session-minutes 120
```

Alternatively: `uv run --locked python -m sahakaram.backend`. There is no MCP
initialization, plugin startup, provider login, database service, or tunnel needed
for local backend operation. SQLite initialization/migration happens automatically.
`uv run --locked sahakaram hub` is a compatibility alias for the same backend.

Choose `SAHAKARAM_STATE_DIR` before startup. The base CLI defaults to `.state`; the
plugin defaults to `~/.sahakaram`. Use one consistent private state directory when
the host plugin needs access to its locally authorized bootstrap flow. A standalone
HTTP client can instead authenticate using the backend's own bootstrap capability.
Do not run multiple backend instances against one database.

## HTTP contract

Health is `/healthz`; readiness is `/readyz`; OpenAPI is `/openapi.json` and API
reference is `/docs`. Base path: `/api/v1`.

| Operation | Endpoint |
| --- | --- |
| Host identity | POST /identities |
| Create room | POST /rooms |
| Redeem invitation | POST /join |
| Read room | GET /room |
| Ordered replay | GET /events?after=CURSOR&limit=50 |
| Live replay | WebSocket /events/ws?after=CURSOR |
| Invite | POST /invitations |
| Message | POST /messages |
| Task | POST /tasks; PATCH /tasks/TASK_ID |
| Human controls | POST /room/state; POST /participants/ID/kick; POST /leave |
| Usage observation | POST /usage |

Host bootstrap operations require `X-Sahakaram-Bootstrap`, using the capability in
`host-bootstrap.key` under the host's private state directory. It grants backend
host authority and must never be exposed to teammates. It does not require the
local Codex profile or plugin to exist. Enrollment returns scoped room credentials;
subsequent room calls use `Authorization: Bearer TOKEN`. The backend checks current
membership/status/token version as well as expiry. `X-Sahakaram-Room` optionally
binds a request to a specific room. Plugin client policy adds local human unlock
before using host bootstrap, but that interface policy is not a backend dependency.

Use message/task UUIDs for identical creation retries, expected versions for task
updates, and next_cursor for event replay. A task assignment records responsibility;
it never starts a model. Errors include structured codes and appropriate HTTP status.
Human admin and agent lead remain separate authorities. Provider credentials never
belong in this API, its database, or room messages.

## Interface boundaries

- `services.py`: collaboration domain and transactions; no transport/plugin imports.
- `hub.py`: HTTP/WebSocket schemas and authentication adapters; no MCP imports.
- `storage/`: SQLite schema, migrations, transactions and UTC conversion.
- `backend.py`: standalone composition, lifecycle and process entrypoint.
- `server.py` and `bridge.py`: optional MCP tools and HTTP client mapping.
- `local_api.py`, `local/`, `adapters/codex.py`: optional local account/runtime control.

The base dependency set excludes MCP, the Codex SDK and PostgreSQL drivers.
Install the MCP interface with `uv sync --locked --extra plugin`; install optional
managed Codex runtimes with `--extra codex`. The distributed plugin selects its own
`plugin` extra automatically. A dashboard can be added without importing MCP or
changing backend business logic.

## Forwarding

Only forward backend port 8788. VS Code's Ports view and Microsoft Dev Tunnels are
supported deployment choices, not imports in backend code. The backend listens on
loopback; callers use the forwarded HTTPS origin or an SSH local forward. Cloudflare
is not required or started. See [the friend walkthrough](friend-demo.md).
