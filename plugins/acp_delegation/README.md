# acp_delegation

Delegate one coding task to an external worker — Claude Code or pi — over the
Agent Client Protocol, using the [`acpx`](https://github.com/openclaw/acpx) CLI.

This is an ANYA-PATCH plugin. It is not upstream Hermes.

## Why acpx and not Hermes' own ACP

`hermes acp` runs Hermes **as** an ACP server, for editors to drive. Delegating
outward needs an ACP *client*, which Hermes does not have: upstream issue
NousResearch/hermes-agent#5257 proposes one and is still open. `acpx` is that
client, and it speaks to the same adapters — `claude-agent-acp`, `pi-acp` — that
Anya already runs.

## Enable it

```bash
npm install -g acpx
hermes plugins enable acp_delegation
hermes gateway restart
```

A restart is required: Hermes assembles its toolset at session start.

Restarting is not free. The drain window is 0 seconds and Slack Socket Mode does
not replay, so a message sent during the restart is lost. Restart when the
channel is quiet.

## Configure it

The plugin refuses to run until `allowed_cwd_roots` is set. That is deliberate:
the alternative default is letting a worker edit anything on the box.

```yaml
plugins:
  entries:
    acp_delegation:
      allowed_cwd_roots:            # required — a task may only run inside these
        - /Users/wegoaiteam/working-repos
      acpx_bin: acpx                # absolute path if acpx is off PATH
      default_timeout_seconds: 900
      max_timeout_seconds: 3600
      max_response_chars: 8000
      kind_policy:                  # acpx --permission-policy
        autoApprove: [read, search, edit]
        autoDeny: []
        escalate: []
        defaultAction: deny
```

## The tool

```
acp_delegate(worker="claude"|"pi", task="...", cwd="/abs/path", timeout_seconds=900)
```

`worker` is required and has no default. There is no automatic fallback between
the two: a failover that guesses is how Anya's own breaker learned to blame one
worker for another's usage cap.

## Two permission layers, because one is not enough

| Layer | Enforced by | Scope |
| --- | --- | --- |
| Kind | `acpx --permission-policy` | which *kinds* of action are allowed |
| Path | Claude Code `settings.local.json` | which *files* may be touched |

Both are needed. An acpx policy matches tool kinds and names but **never paths**,
so approving the `edit` kind approves an edit anywhere on the filesystem — a
worker asked to write outside its working directory produces an ordinary `edit`
request that looks identical to a legitimate one.

So the path rules go where paths are understood. Before each run the plugin
merges deny globs into `<cwd>/.claude/settings.local.json`, which the ACP session
loads as project-scoped settings, and restores the file afterwards. The shared
`~/.claude/settings.json` is never touched.

## Verifying a result

The handler returns `success: false` with `error_type: "false_success"` when the
worker reports completion having produced no text **and** no output tokens.

The token half of that check reads `output_tokens`, never `total_tokens`. A
one-word reply measured 31,206 total tokens because 31,198 of them were cache
writes, so a total-token guard can never fire. `tests/test_parse.py` pins this.

## Error types

| `error_type` | Meaning |
| --- | --- |
| `not_configured` | `allowed_cwd_roots` is unset. Operator action. |
| `invalid_worker` / `invalid_task` / `invalid_cwd` | The call was wrong. Retry differently. |
| `acpx_not_found` / `spawn_failed` | acpx could not be started. |
| `agent_error` | acpx exit 1 — adapter, protocol, or runtime fault. |
| `cli_usage_error` | acpx exit 2. A plugin bug: it built bad arguments. |
| `timeout` | acpx exit 3 — acpx stopped the worker itself. |
| `plugin_deadline_exceeded` | acpx itself stopped responding and was killed. |
| `no_session` | acpx exit 4. |
| `permission_denied` | acpx exit 5 — every action was denied. |
| `interrupted` | acpx exit 130. |
| `malformed_output` | Clean exit with no final result. The output cannot be trusted. |
| `false_success` | Reported completion, did nothing. |
| `unknown_exit` | An exit code acpx does not document. |

## Restart safety

While a task runs, the plugin writes a lease file:

```
$HERMES_HOME/runtime/acp_delegation/active/<id>.json   {"pid": …, "worker": …, "cwd": …}
```

`infra/deploy/safe-restart.sh` in the `wego/openclaw-anya` repo reads that
directory and refuses to bounce the gateway while a pid in it is alive.

It is a lease rather than a process-name match on purpose. Whether a worker
appears in `ps` as `claude` or as `node` depends on how npm resolved the bundled
adapter, and the existing guard silently fail-opened for months for exactly that
reason. The process that starts the work is the one that can name it.

The lease lives under `$HERMES_HOME/runtime`, never inside this directory: a
deploy that rsyncs the plugin tree with `--delete` would erase an in-flight
lease.

## Layout

```
tools.py             the tool: schema + handler. The ONLY module importing Hermes.
config.py            settings + working-directory admission. Pure.
parse.py             NDJSON -> events -> result. Pure, imports nothing.
settings_overlay.py  install/restore the path-level deny rules.
acpx_process.py      spawn, stream, deadline, teardown, lease file.
```

## Tests

Run from this directory. No acpx, no worker, and no credentials are needed.

```bash
python3 -m unittest discover -s tests
```

## Non-goals

- No failover between workers.
- No multi-step pipeline. One task, one call.
- No path-level layer for `pi` — it has no Claude-settings equivalent, so the
  kind policy is its only guard.
