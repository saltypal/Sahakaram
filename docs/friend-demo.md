# A first room with a friend

This walkthrough connects existing Codex agents. Neither side needs the optional
Codex SDK. The host needs PostgreSQL and cloudflared; teammates do not.

## 1. Both people install the plugin

```sh
codex plugin marketplace add saltypal/Sahakaram
codex plugin add sahakaram@sahakaram
```

Reload Codex and call `get_status`. For a client without marketplace support, use
`codex mcp add sahakaram --env SAHAKARAM_STATE_DIR=ABSOLUTE_PRIVATE_STATE_PATH -- uv run --locked --project ABSOLUTE_CLONE_PATH sahakaram mcp`.
The supported packaged layout is documented by
[OpenAI](https://developers.openai.com/plugins/build/plugins).

## 2. Host: clone the code and choose private state

```sh
git clone https://github.com/saltypal/Sahakaram.git
cd Sahakaram
uv sync --locked
```

PowerShell (use in every host terminal):

```powershell
$env:SAHAKARAM_STATE_DIR = Join-Path $HOME '.sahakaram'
```

Bash/zsh:

```sh
export SAHAKARAM_STATE_DIR="$HOME/.sahakaram"
```

This matches the plugin's default state. The source checkout's normal CLI defaults
otherwise to `.state`. Keeping one consistent directory matters: it holds the local
unlock session and host signing/bootstrap keys. Do not share or commit it.

Create a PostgreSQL database for Sahakaram using your normal PostgreSQL tools.
Set its connection URL in the hosting terminal:

```powershell
$env:SAHAKARAM_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/sahakaram'
```

For Bash use `export SAHAKARAM_DATABASE_URL='...'`. Replace the placeholder values;
use an environment variable or secret store, never a committed configuration file.
Only this host needs the database. Then:

```sh
uv run --locked sahakaram db-upgrade
uv run --locked sahakaram setup --displayname Alice
uv run --locked sahakaram unlock
uv run --locked sahakaram hub --session-minutes 120
```

`setup` and `unlock` ask for the local profile passphrase privately in the terminal.
For an existing profile, skip setup and run unlock. Keep the hub terminal open.
The explicit 120-minute bearer lifetime is for this bounded demo; the default is
15 minutes. Revocation still applies immediately. There is no automatic renewal
or expired-session recovery yet, so finish or checkpoint within that interval.

## 3. Host: expose only the room API

Install [cloudflared](https://developers.cloudflare.com/tunnel/downloads/).
In a second terminal, from the checkout:

```sh
uv run --locked sahakaram share
```

It runs `cloudflared tunnel --url http://127.0.0.1:8788 --no-autoupdate` and prints
an HTTPS `trycloudflare.com` URL. Keep this terminal open. It publishes the room
API, not the local account control service or PostgreSQL. Quick Tunnels are
[temporary development endpoints](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
without an uptime guarantee. Sahakaram uses ordinary HTTP requests and event replay;
its friend workflow does not require SSE.

Before sending the invitation, confirm that `HOST_URL/readyz` returns
`{"status":"ready"}`. A newly generated hostname can take time to appear in DNS
even after cloudflared registers its connection. If hostname resolution initially
fails, allow DNS publication to finish before retrying. The release smoke check
verified ordinary HTTPS and an MCP join after DNS became available, without
bypassing the local resolver or TLS verification.

Do not enable interactive email authentication on this tunnel: the MCP bridge is
not a browser. Room calls use Sahakaram invitation/bearer credentials instead.
For a longer-lived deployment use a managed tunnel/private network and stronger
operational controls; that deployment is not provided by this preview.

## 4. Host Codex: create the room and invite

Give your host Codex this prompt:

> Use Sahakaram. Create a room named Pocket Ledger, with my display name Alice,
> account label Lead, and three participants maximum. Generate one agent invite.
> Give me the room ID and the invitation secret to share privately. Do not share
> local profile passphrases, provider credentials, or stored room bearer tokens.

Share the tunnel URL and invitation secret privately with your friend. Each
invitation is one use and expires after 30 minutes by default. Call `create_invites`
again for another teammate. The host keeps its configured hub on loopback; only
teammates select the public URL.

## 5. Teammate Codex: connect and join

Give your friend this prompt, substituting the private invitation and HTTPS URL:

> Use the Sahakaram plugin. Call connect_hub with the HTTPS host supplied by me:
> HOST_URL. Then join_room with invitation_secret INVITATION, my display name Bob,
> and account label Storage. Read the room snapshot and replay its events. Introduce
> yourself in the room, propose your part of Pocket Ledger, and wait until the
> lead assigns the task. Keep all provider credentials and local passphrases local.

`connect_hub` saves the host in private local state; subsequent calls in the same
MCP session use it. `SAHAKARAM_HUB_URL`, if explicitly set in the client's environment,
overrides the saved connection; remove that override when using connect_hub.
Teammates do not need to set up a local profile or database merely to join/chat.
They do need local unlock/provider login if separately using account-management tools.

## 6. Build Pocket Ledger together

Scope: `add`, `list`, and `summary` commands; CSV storage; Decimal money; totals
by category. No banking integration, web UI, or external service.

Before coding, the lead proposes the API, filenames and test cases in the room.
With two agents the lead owns the CLI/integration, and the friend owns storage and
calculations. With three, a third agent takes CLI/docs. The lead uses create_task
and assign_task. Assignees call update_task with the returned expected_version
and `working`, then `review` and a concise result_summary. The lead reviews before
marking done. Use each result's new version for the next update.

The conversation is pull-based: call receive_messages with the last next_cursor
at task boundaries, on blockers, and before integration. Do not loop constantly.
Messages do not automatically wake or run an idle Codex client. Humans should
keep both clients working on the agreed task. Auto-delegation is future work.

Use separate feature branches. Each person's GitHub account must have collaborator
permission on the demo repository to push. Sahakaram itself neither grants GitHub
access nor performs Git operations. Avoid both agents editing the same file.

## 7. Finish and stop

Ask the lead to summarize merged changes, tests, and unfinished work in the room.
The human administrator can pause or close it. Teammates can leave. Stop the share
and hub terminals with Ctrl+C; that removes the temporary public endpoint.
`uv run --locked sahakaram lock` revokes local control sessions and closes managed
Codex runtimes. Room closure does not terminate another person's existing Codex.

## Troubleshooting

- Missing Sahakaram tools: restart/reload Codex after installation; verify uv is
  on the GUI application's PATH, and inspect the MCP startup log.
- Connection refused/502: keep the host API and tunnel running; verify
  `http://127.0.0.1:8788/readyz` locally and `HOST_URL/readyz` through the tunnel.
- Invalid invitation: use a fresh unused invite, with the same tunnel/host that
  created it. Never redeem an invite through an untrusted replacement URL.
- Room full: agents retain their seats while disconnected. Ask the administrator
  to remove an unused teammate. Rejoining with a fresh invite creates another
  registration; it does not recover an expired identity automatically.
- Invalid/expired credential: stop and checkpoint. This preview has no automated
  credential recovery; use a fresh supervised room for the next demo.
- Host setup works but create_room fails: verify that the host CLI and plugin use
  the same SAHAKARAM_STATE_DIR and that local unlock has not expired (one hour).
- Hub restart: active rooms are paused for review; call resume_room as admin.
