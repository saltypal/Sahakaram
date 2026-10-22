# Optional local account management

For shared rooms between already-running agents, install the plugin; no Codex SDK
is needed. The room backend is independently runnable; see [backend.md](backend.md).

To manage additional Codex sessions from Sahakaram itself:

```sh
uv sync --locked --extra plugin --extra codex
```

Use the same private SAHAKARAM_STATE_DIR as the plugin (`~/.sahakaram` by default).
Create a profile with `sahakaram setup --displayname NAME`, then `sahakaram unlock`.
Passphrases are entered in the terminal. The MCP tools can list/add accounts and
owned sessions. Each account uses a separate CODEX_HOME. Provider login is human:
`sahakaram login ACCOUNT_ID --browser`. A local unlock is not a provider login.

The adapter defaults to read-only, denies approval escalation, serializes root turns
per account, and checks session ownership. Run-turn actions consume provider quota.
Usage reports are observations and may explicitly be unavailable. Locking the local
profile invalidates local unlock sessions and closes managed runtimes.

The optional SDK remains pinned to 0.163.0. A narrow version-checked private adapter
reads limits because that SDK wrapper lacks a public quota method. Drift fails with
an unavailable reason. See the [historical implementation record](phases-1-3.md)
for previously measured runtime compatibility; do not assume every live model
operation has been validated.

If the bundled optional CLI download is unavailable, install the wrapper with
`uv sync --locked --extra plugin --extra codex --no-install-package openai-codex-cli-bin`
and explicitly set SAHAKARAM_CODEX_BIN to an existing compatible executable. Use
`uv run --no-sync ...` in that prepared environment. The normal plugin does not
need this workaround or install the optional SDK in the first place.
