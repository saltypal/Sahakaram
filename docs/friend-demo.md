# A first room with a friend

This walkthrough connects existing Codex agents. Neither side needs the optional
Codex SDK. The host runs a standalone SQLite backend. No database server is needed.

## 1. Both people install the plugin

```sh
codex plugin marketplace add saltypal/Sahakaram
codex plugin add sahakaram@sahakaram
```

Reload Codex and call `get_status`. For a client without marketplace support, use
`codex mcp add sahakaram --env SAHAKARAM_STATE_DIR=ABSOLUTE_PRIVATE_STATE_PATH -- uv run --locked --project ABSOLUTE_CLONE_PATH --extra plugin sahakaram mcp`.
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

Start the backend; it creates and migrates the local SQLite file automatically:

```sh
uv run --locked sahakaram-backend --session-minutes 120
```

Keep that terminal open. The explicit 120-minute credential lifetime is for this
bounded demo; the default is 15 minutes. Revocation still applies immediately.
The existing PostgreSQL database, if any, is untouched and is not imported.

In another terminal using the same private state directory, authorize the plugin's
host actions (base CLI does not need MCP to manage the local profile):

```sh
uv run --locked sahakaram setup --displayname Alice
uv run --locked sahakaram unlock
```

Passphrases stay in the terminal. Skip setup for an existing profile. The standalone
backend does not require a Codex profile; this unlock belongs to the host plugin's
local authorization flow. See [backend.md](backend.md) for direct HTTP clients.

## 3. Host: Dev Tunnels or port forwarding

### Option A: VS Code Ports view

Open VS Code, open the Ports view, and forward port **8788**. Sign in with your
GitHub/Microsoft account if prompted. For this invitation-based demo, make that
forwarded port Public and copy its HTTPS forwarding URL. This permits requests to
reach the API; Sahakaram still requires its own invitations and room credentials.
A private browser-authenticated port will redirect a friend's MCP client to login.

Do not forward port 8787 (local account control) or share the SQLite file.
Follow [VS Code port forwarding documentation](https://code.visualstudio.com/docs/debugtest/port-forwarding).

### Option B: Microsoft Dev Tunnels CLI

Install [devtunnel](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/get-started)
and log in yourself:

```sh
devtunnel user login -g
uv run --locked sahakaram share
```

The wrapper runs `devtunnel host -p 8788 --allow-anonymous`. It does not install the
CLI or perform account login. Keep the host terminal open and copy the HTTPS URL.
The transport allows anonymous requests, while Sahakaram's API still enforces
invitation and bearer authorization. No Microsoft/GitHub login token is sent in
room messages. See [Microsoft's command reference](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/cli-commands).

Before inviting your teammate, check `HOST_URL/readyz` returns `{"status":"ready"}`.
API clients request JSON to avoid the tunnel's HTML anti-phishing interstitial.
If using a private Dev Tunnel, separate tunnel authentication must be configured;
the simple walkthrough uses public transport with Sahakaram authorization.

### Option C: an existing SSH local forward

If your friend already has SSH access to the host, they can run:

```sh
ssh -N -L 8789:127.0.0.1:8788 USER@HOST
```

They then call connect_hub with `http://127.0.0.1:8789`. HTTP loopback is accepted;
remote origins require HTTPS. The SSH connection supplies transport encryption.
Do not expose an unauthenticated plaintext backend port directly to the internet.

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
The human administrator can pause or close it. Teammates can leave. Stop the forwarding
and backend terminals with Ctrl+C to end access.
`uv run --locked sahakaram lock` revokes local control sessions and closes managed
Codex runtimes. Room closure does not terminate another person's existing Codex.

## Troubleshooting

- Missing Sahakaram tools: restart/reload Codex after installation; verify uv is
  on the GUI application's PATH, and inspect the MCP startup log.
- Connection refused/502: keep the backend and forwarding session running; verify
  `http://127.0.0.1:8788/readyz` locally and `HOST_URL/readyz` through the tunnel.
- Invalid invitation: use a fresh unused invite, with the same forwarded host that
  created it. Never redeem an invite through an untrusted replacement URL.
- Room full: agents retain their seats while disconnected. Ask the administrator
  to remove an unused teammate. Rejoining with a fresh invite creates another
  registration; it does not recover an expired identity automatically.
- Invalid/expired credential: stop and checkpoint. This preview has no automated
  credential recovery; use a fresh supervised room for the next demo.
- Host setup works but create_room fails: verify that the host CLI and plugin use
  the same SAHAKARAM_STATE_DIR and that local unlock has not expired (one hour).
- Hub restart: active rooms are paused for review; call resume_room as admin.
