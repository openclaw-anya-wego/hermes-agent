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
      max_response_chars: 100000
      project_markers:              # what makes a directory a project root
        - .git                      # default: .git, .hg, .svn
      kind_policy:                  # acpx --permission-policy
        autoApprove: [read, search, edit]
        autoDeny: []
        escalate: []
        defaultAction: deny
```

## One root, many projects

`allowed_cwd_roots` answers *may the worker touch this at all*. It does not say
which project a task runs in — a root normally holds dozens, and each delegation
targets a different one. So the project root is resolved **per request**, by
walking up from the path given until a `project_markers` entry is found.

| Request | Runs in |
| --- | --- |
| `…/working-repos/falcon` | `…/working-repos/falcon` |
| `…/working-repos/falcon/src/app` | `…/working-repos/falcon` |
| `…/working-repos` | refused — `invalid_cwd` |

That last row is the point. The worker's cwd decides whose settings, agents,
hooks and MCP servers it loads, so a task run at the directory that merely
*contains* the checkouts gets none of the project's configuration — and nothing
about the result says so. Refusing is the only outcome that is not silent. The
returned `cwd` always reports where the worker actually ran.

The walk never goes above the allowed root that admitted the path, so anchoring
cannot escape the boundary.

Markers are **version-control roots, not agent config**. Anchoring on `.claude/`
would resolve one path differently per worker: a monorepo package carrying Claude
settings but no pi settings would be the project root for `claude` and not for
`pi`, and every new tool would need another entry. A repository root means the
same thing to all of them. Projects that are not checkouts are what
`project_markers` is for.

## The tool

```
acp_delegate(worker="claude"|"pi", task="...", cwd="/abs/path",
             command="/saber-code-review #1234", timeout_seconds=900)
```

`worker` is required and has no default. There is no automatic fallback between
the two: a failover that guesses is how Anya's own breaker learned to blame one
worker for another's usage cap.

### command and task are separate on purpose

The prompt sent to the worker is `command + "\n" + task` — the shape
`skills/worker-delegate/spawn.md` has been sending in production. The procedure
lives in the command file on the worker; `task` supplies only what that procedure
needs.

A dedicated field rather than "start your task with a slash command" is the
finding from CLAUDE.md rule 10, measured over five live runs: every **template
slot** was filled correctly, and every requirement written as **prose inside a
step** fired zero times. A schema property is a slot. A sentence in a description
is prose — which is also why the single-line shape is enforced in the handler and
not merely described.

`command` also covers **skills**, because a user-invocable skill is a slash
command. It does not cover **subagents**: there is no `/agent-name` surface, so
the only way to make one run is a command file that invokes it. A tool argument
named `agent` would promise something only prose could attempt, and this plugin
already refuses that shape once — see `_deny_rules_for`.

Commands resolve from the project, so one that exists in a given checkout may not
exist in another. A command the worker does not have fails nowhere on its own: it
arrives as ordinary prose, the worker improvises around it and exits 0, and the
reply is indistinguishable from one that followed the procedure.

So the plugin checks. The worker advertises its commands over ACP
(`available_commands_update`), the parser collects them, and a run whose command
is not in that list comes back `success: false` with
`error_type: "unknown_command"`, the names that do exist, and the worker's reply
kept so the work is not lost. This is the same judgement as `false_success`: a
reply produced without the procedure that was asked for is not a success, however
plausible it reads.

It **fails open when the worker advertised nothing**. An empty list means the
worker never said, which is not evidence it has no commands — pi may never send
the event at all.

## The worker runs in `auto` mode

Each delegation opens a **named acpx session**, sets its mode, prompts, then
closes it. The mode is the reason for the session — `exec` is one-shot and
cannot carry one.

```yaml
permission_mode: auto     # default; "default" restores prompt-everything
```

Without it a review cannot run at all. Reviewing code means running `git`, `gh`
and tests, which are the ACP `execute` kind, and the kind policy below denies
`execute`. It has to: acpx matches **kinds, never paths**, so allowing "run
git" there also allows "run anything, anywhere".

`auto` moves that judgement into the worker, whose classifier decides per action
and mostly never asks acpx at all.

**The two gates are sequential, not additive** — and that is the part that is
easy to get backwards. A worker only asks acpx about what it did not settle
itself, so the kind policy is the *backstop* for escalations, not the first
word. That is also why `~/.claude/settings.json` looked ignored: its
`permissions` rules do apply, but its `permissionMode` is read by the
interactive CLI, not by the SDK the ACP adapter drives. acpx sets no mode of its
own, so before this every session ran at the adapter's `default` and asked about
everything.

Session names are `acp-<run id>`, shared with the lease and the settings
overlay so a run's three artefacts can be traced to each other. Teardown is
best-effort: a leaked session costs a stale record, not a run. Note that
`sessions close` takes the name **positionally** — passing it with `-s` is
accepted and closes nothing.

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
merges deny globs into `<project root>/.claude/settings.local.json`, which the
ACP session loads as project-scoped settings, and withdraws them afterwards. The
shared `~/.claude/settings.json` is never touched.

That the session reads them is not an assumption: the adapter passes
`settingSources: ["user", "project", "local"]` and `cwd: params.cwd`
(`claude-agent-acp/src/acp-agent.ts:4830` and `:4852`). Its own docs do not
mention settings, which is why this is cited from source.

### The file belongs to the operator

It sits in a checkout they also work in, so every rule this plugin adds is
labelled with who added it, under a `_acp_delegation` key. Restore works by that
marker rather than by restoring a snapshot, because a snapshot only survives
while there is exactly one writer and nothing interrupts it:

- **Killed mid-run** — a gateway bounce, a reboot — a snapshot dies with the
  process, leaving the deny rules in the operator's repository forever and
  silently applying to their own interactive sessions. The next install in that
  directory prunes whatever a dead run left, so this self-heals. `grep -r
  _acp_delegation` finds any that are still sitting there.
- **Two delegations in one checkout** — each run records the rules it *requires*,
  and a rule is withdrawn only once no remaining run requires it. Finishing first
  cannot disarm a run that is still going, in either order.

Rules the repository denies on its own are honoured but never claimed, so no
restore can take away something the operator wrote. Restore preserves the file's
*content*, not its bytes: it is regenerated from what is on disk at the time, so
an edit made while the worker ran survives and the indentation becomes ours.

### This is a denylist, not a sandbox

Read this before trusting it. The path layer closes the directories that let a
delegated task **escalate** — rewrite the agents, read a credential, or arrange
to run again later:

| Group | Paths |
| --- | --- |
| Agent config | `~/.openclaw`, `~/.hermes`, `~/clawd`, `~/.claude` |
| Credentials | `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/Library/Keychains` |
| Startup | `~/Library/LaunchAgents`, `/etc`, `/Library`, shell rc files, `~/.gitconfig` |

It does **not** confine writes to `cwd`. A task that names some other repository
under the operator's home will be allowed to edit it. Doing better needs a
permission decision that can see the path, which means the ACP Python SDK's
`request_permission` callback rather than acpx. Until that lands, do not
describe this tool as sandboxed.

The rules are applied for `claude` only. Writing Claude Code's settings format
for a `pi` delegation would drop a file into the checkout that `pi` ignores — a
gesture that reads as a guard and is not one.

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
| `invalid_worker` / `invalid_task` / `invalid_cwd` / `invalid_command` | The call was wrong. Retry differently. |
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
| `unknown_command` | The worker has no such command. It improvised; the reply is not the procedure. |
| `unknown_exit` | An exit code acpx does not document. |

## Progress while it runs

This call blocks for as long as the delegation takes — up to 90 minutes. Without
progress the host sees one tool call and no activity, and cannot tell a working
worker from a hung one.

So the stdout reader watches the worker's `tool_call` and `tool_call_update`
events and reports the newest one. The adapter's own `title` is used verbatim: it
is already written for a human, and it is the same string an editor would
display.

Two surfaces get the same sentence and deliberately **different cadences**:

| Surface | Fires on | Rate |
| --- | --- | --- |
| Live status line | the action **changing** | 5 s |
| Host activity clock | **any** worker output | 60 s |

They are not interchangeable. The status line is read by a human, so repeating a
phrase is flicker rather than information. The activity clock is a liveness
proof for the gateway's inactivity watchdog — `agent.gateway_timeout`, which
warns at 15 minutes and abandons the turn at 30 — and there a repeat still
counts.

Reporting only on change is what made a live 26-minute delegation get *"⚠️ No
activity for 15 min"* at 23 minutes: the worker had settled into one long step,
the title stopped changing, and the clock went stale while it worked. A
delegation may run for 90 minutes, so the clock is the only thing keeping the
turn alive.

The clock is driven by the worker's own output, never by a timer. A timer would
tick forever and mask a genuinely hung worker, which is the fault the watchdog
exists to catch — so an acpx that goes truly silent still times out.

The status line needs the core patch described in `ANYA-PATCHES.md`; without it
the plugin still feeds the activity clock and the line stays generic.

**Adding a worker changes nothing here.** The phrase interpolates the `worker`
argument rather than branching on it, and the events come from ACP, which every
reachable worker speaks: `codex worker: Edit Fare.java…` needs no code.

- **`completed` updates are ignored** for the status line. Reporting a finished
  step would show it for however long the next one takes — reading as a stall
  exactly when the worker is busiest.
- **The phrase is truncated** to `STATUS_PHRASE_MAX_CHARS`. Nothing between the
  host and the connector truncates, and the action text comes off the wire.
- **Nothing reports once the run ends.** On the deadline path a reader outlives
  the call, and a late phrase would paint a dead worker over the next tool.
- **Entirely optional.** No host callback means no reporting; the tests run this
  path offline.

Both callbacks are captured in `tools.py`, on the handler's thread, because the
host's are thread-local and a reader thread cannot look them up for itself. They
are passed in as `HostProgress` — which is what keeps every other module in this
plugin free of Hermes imports. Failures are swallowed: an exception on the
stdout reader would stop the only thread draining that pipe and hang the worker
on a full buffer — a hang caused by the code proving there is none.

Both workers are covered. `pi` emits the same ACP events, so nothing here is
claude-specific.

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
