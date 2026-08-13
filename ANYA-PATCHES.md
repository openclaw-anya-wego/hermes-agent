# ANYA-PATCHES

This file holds the Anya-specific setup for the Hermes agent.

## Branch rules

- **`main` is the working branch.** Make all changes on `main`. The mini builds
  and runs `main`.
- The fork is `openclaw-anya-wego/hermes-agent`.
- `anya-patches` is retired. It was the working branch until 2026-08-13, when
  everything on it merged into `main`.

### Taking upstream changes

`main` is no longer a clean mirror of `NousResearch/hermes-agent`, so upstream
arrives by merge rather than by fast-forward:

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git   # once
git fetch upstream
git merge upstream/main
```

Expect conflicts in the files listed under *Patches we carry* below. That
is the cost of running work on `main`: the previous layout answered "what did we
change?" with `git diff main..anya-patches`, and now the answer is this file.
Keep it current — it is the only remaining record of what is ours.

## Install

| Item | Value |
| --- | --- |
| Host | `anyaminimac` (Mac mini M4, user `wegoaiteam`) |
| Directory | `/Users/wegoaiteam/hermes-agent` |
| Version | Hermes Agent v0.20.0 (2026.8.3) |
| Python | 3.11.15 in `venv/` |
| OpenAI SDK | 2.24.0 |
| Skills | 79 bundled |

To install again, run `./setup-hermes.sh` from the repository root.

### Python version constraint

The mini has Python 3.14.6. Hermes requires Python `>=3.11,<3.14`.
The setup script makes a Python 3.11 virtual environment in `venv/`.
Do not run Hermes with the system Python. The build of `pydantic-core` fails on Python 3.14.

## Memory: use Anya's Honcho

Do not install a second Honcho instance. Anya runs Honcho on this same host.
A second instance costs about 2.6 GB of RAM. It also puts two derivers on one database.

Set these values instead:

```
HONCHO_BASE_URL=http://localhost:8000
workspace_id: openclaw
```

The Honcho server runs with `AUTH_USE_AUTH=false`. A token is not necessary. Leave it empty.

Hermes reads `HONCHO_BASE_URL` in `plugins/memory/honcho/client.py`.
The key `honcho.base_url` in `config.yaml` has the same effect.
The command `hermes memory setup` asks for these values.

Anya's Honcho listens on port 8000 inside Colima. Colima forwards the port to the Mac.
The address `http://localhost:8000` works from a native install.

## Wiki access

Anya's wiki is a markdown vault at `/Users/wegoaiteam/.openclaw/wiki/main`.

Read the files directly. Do not write to the vault.
The OpenClaw tool `wiki_apply` keeps the link graph and the claim-ownership index.
A direct write makes these indexes incorrect.

## Do not use Docker on this host

The file `docker-compose.yml` sets `network_mode: host` on both services.
Colima runs a Linux virtual machine. Under Colima, `host` means the guest network.
The dashboard port is then unreachable from macOS. Install natively instead.

## Status

The install is complete. Hermes is not configured. Hermes is not running.

Remaining steps:

1. Add a model API key to `.env`.
2. Set the Honcho values above.
3. Start Hermes.

### RAM constraint

The mini has 16 GB of RAM. The box is oversubscribed.
Hermes needs about 400 MB to 700 MB for two Python processes.
Free memory before you start Hermes. A reboot clears the swap and the accumulated leaks.

## Additions we own

Local code that is not a patch. It fixes nothing upstream and will never be sent
there, so it does not belong in the patch list below.

### `plugins/acp_delegation/`

Lets Hermes delegate a coding task to the Claude Code worker or pi over the
Agent Client Protocol, by calling the `acpx` CLI. Migration stage 5.

Hermes cannot do this on its own. `hermes acp` runs Hermes **as** an ACP server
for an editor to drive; delegating outward needs an ACP *client*, which upstream
issue NousResearch/hermes-agent#5257 proposes and has not built. That issue
would also not solve this: it is shaped as a *provider* shim — running Hermes on
Claude Code as a model — rather than handing a task to Claude Code's own agent
loop. The existing `copilot_acp_client.py` is a provider shim of exactly that
kind.

Enable with `hermes plugins enable acp_delegation`; a bundled standalone plugin
is still opt-in (`hermes_cli/plugins.py:1468`). It refuses to run until
`plugins.entries.acp_delegation.allowed_cwd_roots` is set.

See `plugins/acp_delegation/README.md` for configuration, the two permission
layers, and the error taxonomy.

#### Live status for a long-running tool

Three core files carry a small addition so `acp_delegate` can say what it is
doing. Expect conflicts here on an upstream merge:

| File | Change |
| --- | --- |
| `tools/environments/base.py` | `set_status_callback` / `get_status_callback`, thread-local, mirroring the activity pair beside them |
| `agent/tool_executor.py` | registers one per tool call, pointed at that tool's name |
| `gateway/run.py` | renders a `tool.status` event verbatim onto the live status line |

Three files, and deliberately not a fourth. The Slack adapter is **not** patched:
the phrase rides upstream's existing `status` argument to
`assistant.threads.setStatus`, which renders in the footer beneath the reply
composer.

### Why the inline indicator is left alone

While an AI app works, Slack shows a *second* indicator inline in the message
list — "Processing…", "Searching…". It is fed by the same call's
`loading_messages` argument, so an app *can* control it, and we briefly did.

It was reverted. The reasons are worth keeping, because the surface looks
attractive and is not:

- It caps each message at **50 characters** and **rejects the whole call** at 51
  (`invalid_arguments`, "must be less than 51 characters"), rather than
  truncating. A delegation phrase spends 15 characters on `<worker> worker: `
  before naming a tool or a path, so real phrases exceed it — and the rejection
  silently drops the status entirely.
- The limit is undocumented. It was found by probing the live API, which means
  nothing warns when it changes.
- Using it meant patching a fourth core file, and every core file patched is a
  rebase conflict forever.

The footer surface accepted 300 characters in the same probe, needs no patch at
all, and upstream already documents the inline text as Slack's own.

The status line is otherwise rendered once, from the tool name, when a tool
starts. That suits a tool returning in seconds and fails one that blocks for an
hour: the operator sees `is using acp_delegate…` frozen for the whole run and
cannot tell work from a hang. With this the line reads
`Delegating task to claude worker…`, then tracks the worker —
`claude worker: Run bun test…`.

Nothing in the path names a worker. The phrase interpolates whatever `worker`
was asked for, so a new one needs no change here.

The same events also keep the host's activity clock warm, which is what stops
`agent.gateway_timeout` abandoning a delegation that is working normally. That
clock is fed on **any** worker output rather than only on a change of action —
see the plugin README. It is not fed by a timer, so a genuinely silent worker
still times out as intended.

## Patches we carry

Each patch carries a fix that is already open upstream. Delete the patch when
upstream merges the fix.

### 1. honcho_search returns nothing on a self-hosted Honcho server

- File: `plugins/memory/honcho/session.py`, method `search_context`
- Upstream issue: NousResearch/hermes-agent#79299
- Related server bug: plastic-labs/honcho#940, fix PR #941 (still OPEN)

**Symptom.** `honcho_search` always answers `No relevant context found.`, even
when the same query against `POST /v3/workspaces/{id}/search` returns matches.
The agent then reports that it has no memory.

**Cause.** Two faults combine:

1. `peer_perspective` is a temporal filter (`joined_at <= created_at <= left_at`).
   Hermes calls `add_peers` on every process start when its cache misses. Honcho
   server 3.0.12 and older resets `joined_at` to the current time when it re-adds
   a member that is already active. All history before the last restart becomes
   invisible.
2. The fallback to peer-authored search fired only on an exception. An empty
   HTTP 200 is not an exception, so the fallback never ran.

**Fix.** Fall back to peer-authored search when the result is empty, not only
when the call raises. Also log the resolved `peer_id`, because an unresolved
peer and an empty history produced the same output.

**Test.**

```
hermes -z Call honcho_search with query wiki dreaming deploy. Report the RAW tool output verbatim, nothing else.
```

Before the patch the result is `No relevant context found.`. After the patch the
result is a list of message excerpts.

**Do not upgrade the Honcho server to fix this.** The server fix is not merged.
The bug is present in 3.0.11, 3.0.12, and `main`.

### Known, not yet patched

| Problem | Upstream |
| --- | --- |
| `saveMessages: false` is ignored, so messages are written anyway | #72708, #81214 |
| No noise filtering before write (OpenClaw filters `NO_REPLY` and internal markers) | none found |
