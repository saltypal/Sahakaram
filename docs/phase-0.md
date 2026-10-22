# Phase 0: Python MCP foundation

Implemented on 20 October 2026. This is the first increment, not the room system.

## What happens on a request

1. A client starts `uv run --locked --project <checkout> sahakaram mcp`.
2. The CLI validates SAHAKARAM_* environment settings and installs stderr logging.
3. The official SDK starts the stdio transport and negotiates the MCP connection.
4. The client discovers the single `get_status` tool and calls it with `{}`.
5. The handler returns a typed Pydantic result; the SDK serializes its structured
   output for the client. Application logs never belong on stdout.
6. The subprocess stops when the client closes the transport.

The MCP client here is the coding application (or the test client). The LLM chooses
which tool to request; the Python SDK implements the transport and request handling.
No inference runs inside this server.

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Validate instance name and logging level from the environment |
| `logging.py` | Configure stderr logging only at application startup |
| `server.py` | Create the MCP server and register its structured status tool |
| `cli.py` | Expose `version` and `mcp`; reject invalid configuration without logging values |
| `__main__.py` | Support launching with `python -m sahakaram` |

The server factory receives its settings explicitly. Importing a module neither
starts a server nor changes global logging. No browser, database, provider account,
tunnel, or background companion starts during Phase 0.

## Verified

- Python 3.12.13, official MCP SDK 2.3.0; exact dependencies are in `uv.lock`.
- Seven automated tests pass, including a real subprocess connected through the
  official MCP client, discovery, server version, structured tool results, shutdown,
  classic initialization, JSON-only protocol stdout, and invalid configuration.
- Ruff lint and formatting checks pass.
- Strict mypy checking passes for all six source modules.
- Source distribution and wheel build successfully. The checked archives contain
  no local runtime directories or development caches.
- CLI version/help work when invoked from outside this checkout using `--project`.
- Codex CLI 0.149.0 accepts the proposed stdio launch configuration through
  `codex mcp get sahakaram --json`, with command-line configuration overrides and a
  separate empty CODEX_HOME under ignored runtime storage.
- The lockfile uses the public pypi.org registry.

Reproduce the application checks from the checkout:

```powershell
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build
```

## Remaining checkpoint

The existing Codex application's configuration was not edited, and no authenticated
model turn was launched. Acceptance of a configuration is not evidence of a live
Codex model calling the tool. The README provides the registration example; after
the user adds it and reconnects the client, ask Codex to call `get_status` and compare
the returned service, version, phase, and implemented features.

This live-client check completes the original Phase 0 acceptance gate. Until then,
the foundation is implemented and protocol-tested, with native Codex invocation
still unverified. Phase 1 has not started.
