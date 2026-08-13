"""The ``acp_delegate`` tool: hand a coding task to an external ACP worker.

This is the only module in the plugin that imports Hermes. Everything it calls
takes and returns plain data, which is what keeps the parsing, permission, and
configuration rules testable without a Hermes install.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

from tools.registry import tool_error, tool_result

from plugins.acp_delegation import acpx_process, config, parse, settings_overlay

SUPPORTED_WORKERS = ["claude", "pi"]

# A slash command must be a single token-led line. The shape is enforced here
# rather than described in the schema because the split between `command` and
# `task` only pays off if it cannot be collapsed back: a multi-line value becomes
# a second instruction competing with `task`, and nothing in the result would say
# which one the worker followed.
COMMAND_PATTERN = re.compile(r"^/\S+")

# Path-level denies, enforced by the worker's own settings because acpx matches
# tool kinds and never paths.
#
# This is a DENYLIST, and a denylist is not confinement. acpx auto-approves the
# `edit` kind, so a task that names a path outside `cwd` produces an ordinary
# edit request that is approved unless it matches one of these globs. What this
# buys is that the paths which would let a delegated task escalate — rewrite the
# agents, steal a credential, or arrange to run again later — are closed.
#
# Confining writes to `cwd` needs a permission decision that can see the path,
# which means the ACP Python SDK's request_permission callback. Until then, do
# not describe this as a sandbox.
_AGENT_TREES = ["~/.openclaw", "~/.hermes", "~/clawd", "~/.claude"]
_CREDENTIAL_TREES = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/Library/Keychains"]
_STARTUP_TREES = ["~/Library/LaunchAgents", "~/Library/LaunchDaemons", "/etc", "/Library"]
_STARTUP_FILES = ["~/.zshrc", "~/.zprofile", "~/.bashrc", "~/.bash_profile", "~/.gitconfig"]


def _build_deny_rules():
    rules = []
    for tree in _AGENT_TREES + _CREDENTIAL_TREES + _STARTUP_TREES:
        rules.append("Edit({}/**)".format(tree))
        rules.append("Write({}/**)".format(tree))
    for path in _STARTUP_FILES:
        rules.append("Edit({})".format(path))
        rules.append("Write({})".format(path))
    # Reading a credential is as damaging as writing one, and the worker relays
    # what it reads back into a Slack thread.
    for tree in _CREDENTIAL_TREES:
        rules.append("Read({}/**)".format(tree))
    rules.append("Read(~/.openclaw/gateway.systemd.env)")
    return rules


DEFAULT_DENY_RULES = _build_deny_rules()

ACP_DELEGATE_SCHEMA = {
    "name": "acp_delegate",
    "description": (
        "Delegate one coding task to an external worker (Claude Code or pi) over the Agent "
        "Client Protocol, and return what it did. The worker runs to completion on its own; "
        "it cannot ask follow-up questions, so state the task fully. It may read, search, and "
        "edit files under the given directory only. Use this for real code changes in a "
        "checkout, not for questions you can answer yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "worker": {
                "type": "string",
                "enum": SUPPORTED_WORKERS,
                "description": (
                    "Which worker to use. There is no automatic fallback between them: if the "
                    "one you pick is unavailable, the call fails and you choose again."
                ),
            },
            "command": {
                "type": "string",
                "pattern": "^/\\S+",
                "description": (
                    "Optional slash command for the worker to run, with its arguments — e.g. "
                    "'/saber-code-review #1234'. This also invokes a skill, since user-invocable "
                    "skills are slash commands. The procedure lives in the command file on the "
                    "worker: name it here and do NOT restate its steps in 'task'. One line only. "
                    "Commands resolve from the project, so one that exists in a given checkout "
                    "may not exist in another."
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "The brief: what to change, where, and what done looks like. With a "
                    "'command', this is the context that command needs — the reference, the "
                    "constraints, the acceptance criteria — not a copy of its procedure. "
                    "Include everything the worker needs, because it cannot ask."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Absolute path inside the project the worker should work in. Any path within "
                    "the checkout will do — it is resolved up to the project root, so the worker "
                    "picks up that project's own configuration. Must be inside a directory the "
                    "operator has approved. The returned 'cwd' says where it actually ran."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": config.MIN_TIMEOUT_SECONDS,
                "description": "Wall-clock limit. Defaults to 900 and is clamped to the configured maximum.",
            },
        },
        "required": ["worker", "task", "cwd"],
    },
}


def handle_acp_delegate(args: Dict[str, Any], **kwargs) -> str:
    """Run one delegation. Always returns a JSON string, never raises."""
    del kwargs

    try:
        settings = config.load_settings(_load_hermes_config())
        request = _validate(args, settings)
    except config.ConfigurationError as error:
        return tool_error(str(error), error_type=error.error_type, success=False)

    overlay = None
    started_at = time.monotonic()
    # One id for the run, shared by the lease and the settings overlay, so the
    # two artefacts a delegation leaves on disk can be traced to each other.
    run_id = uuid.uuid4().hex
    try:
        overlay = settings_overlay.install(
            request["cwd"], _deny_rules_for(request["worker"]), run_id
        )
        outcome = acpx_process.run(
            acpx_bin=settings.acpx_bin,
            worker=request["worker"],
            task=request["prompt"],
            working_directory=request["cwd"],
            timeout_seconds=request["timeout_seconds"],
            kind_policy=settings.kind_policy,
            grace_seconds=config.DEADLINE_GRACE_SECONDS,
            lease_id=run_id,
        )
    except acpx_process.SpawnError as error:
        return tool_error(str(error), error_type=error.error_type, success=False)
    except Exception as error:  # noqa: BLE001 - the handler contract forbids raising
        # install() writes into the working tree, so a read-only mount, a
        # permission problem, or a full disk raises here. Hermes requires a JSON
        # string on every path, so the last resort still has to be one.
        return tool_error(
            "Delegation failed before the worker ran: {}: {}".format(
                type(error).__name__, error
            ),
            error_type="delegation_failed",
            success=False,
        )
    finally:
        settings_overlay.restore(overlay)

    return _format(outcome, request, settings, time.monotonic() - started_at)


def acpx_is_available() -> bool:
    """Registry check_fn: is there an acpx binary to call at all?

    Cached by the registry, so this stays cheap enough to run per turn.
    """
    try:
        settings = config.load_settings(_load_hermes_config())
        binary = settings.acpx_bin
    except config.ConfigurationError:
        binary = "acpx"

    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return _found_on_path(binary)


def _validate(args: Dict[str, Any], settings: config.Settings) -> Dict[str, Any]:
    worker = (args.get("worker") or "").strip()
    if worker not in SUPPORTED_WORKERS:
        raise config.ConfigurationError(
            "worker must be one of {}.".format(", ".join(SUPPORTED_WORKERS)), "invalid_worker"
        )

    task = (args.get("task") or "").strip()
    if not task:
        raise config.ConfigurationError("task is required and cannot be empty.", "invalid_task")

    command = _validated_command(args.get("command"))

    return {
        "worker": worker,
        "command": command,
        "task": task,
        "prompt": _compose_prompt(command, task),
        "cwd": config.resolve_working_directory(
            args.get("cwd") or "", settings.allowed_cwd_roots, settings.project_markers
        ),
        "timeout_seconds": settings.clamp_timeout(args.get("timeout_seconds")),
    }


def _validated_command(raw: Any) -> Optional[str]:
    """Accept a single slash-command line, or nothing at all.

    Absent is normal — a plain brief is a valid delegation. What is refused is a
    value shaped like something else, because a command the worker cannot expand
    is pasted into the prompt verbatim and improvised around, which reads as
    success.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise config.ConfigurationError("command must be a string.", "invalid_command")

    command = raw.strip()
    if not command:
        return None
    if "\n" in command or "\r" in command:
        raise config.ConfigurationError(
            "command must be a single line. Put the brief in 'task'.", "invalid_command"
        )
    if not COMMAND_PATTERN.match(command):
        raise config.ConfigurationError(
            "command must start with '/' followed by the command name, "
            "e.g. '/saber-code-review #1234'.",
            "invalid_command",
        )
    return command


def _compose_prompt(command: Optional[str], task: str) -> str:
    """The command first, then the brief — the shape the worker expands.

    Matches what `worker-delegate/spawn.md` has been sending in production: the
    procedure comes from the command file on the worker, and the brief supplies
    only what that procedure needs.
    """
    return "{}\n{}".format(command, task) if command else task


def _format(
    outcome: acpx_process.RunOutcome,
    request: Dict[str, Any],
    settings: config.Settings,
    elapsed_seconds: float,
) -> str:
    result = parse.build_result(
        outcome.transcript,
        outcome.exit_code,
        settings.max_response_chars,
        outcome.stderr_tail,
    )
    result["worker"] = request["worker"]
    result["cwd"] = request["cwd"]
    result["duration_seconds"] = round(elapsed_seconds, 1)
    if request["command"]:
        # Echoed so a result can be read against what was asked for. Commands
        # resolve per project, so "which command ran" is not inferable from the
        # call site alone.
        result["command"] = request["command"]

    result = _flag_unknown_command(result, request["command"], outcome.transcript)

    # tool_result accepts a dict or keyword arguments, never both, so "success"
    # has to travel inside the payload rather than alongside it.
    if result.get("success"):
        return tool_result(result)

    message = result.pop("error", "The delegation failed.")
    return tool_error(message, **result)


# How many command names to list back when one is not found. Enough to correct a
# typo; not the worker's whole catalogue, which can run to dozens.
_SUGGESTION_LIMIT = 20


def _flag_unknown_command(
    result: Dict[str, Any], command: Optional[str], transcript: parse.Transcript
) -> Dict[str, Any]:
    """Fail a run whose command the worker never had.

    An unexpandable slash command is not an error anywhere: the worker receives
    it as ordinary prose, improvises something plausible around it, and exits
    cleanly. The requested procedure never ran, and the reply reads exactly like
    one that followed it — the same false confidence ``false_success`` exists to
    catch, and the reason this is a failure rather than a warning.

    Fails open when the worker advertised nothing. An empty list means it never
    said, which is not evidence that it has no commands, and guessing the other
    way would break every worker whose adapter does not send the event.
    """
    if not command or not result.get("success") or not transcript.available_commands:
        return result

    name = command.lstrip("/").split()[0]
    if name in transcript.available_commands:
        return result

    known = sorted(transcript.available_commands)
    flagged = dict(result)
    flagged["success"] = False
    flagged["error_type"] = "unknown_command"
    flagged["error"] = (
        "The worker has no command '{}'. It read the request as plain text, so the "
        "procedure you asked for did not run and the reply below is improvised. "
        "Commands it does have: {}{}.".format(
            command.split()[0],
            ", ".join(known[:_SUGGESTION_LIMIT]),
            "" if len(known) <= _SUGGESTION_LIMIT else ", …",
        )
    )
    flagged["available_commands"] = known
    return flagged


def _deny_rules_for(worker: str) -> List[str]:
    """Path rules only for a worker that reads them.

    `settings.local.json` is Claude Code's format. Writing it for a `pi`
    delegation would drop a file into the operator's checkout that `pi` ignores
    — a gesture that reads as a guard and is not one, which is worse than no
    guard because someone will trust it.
    """
    if worker != "claude":
        return []
    return DEFAULT_DENY_RULES


def _load_hermes_config() -> Dict[str, Any]:
    """Read Hermes config, treating an unreadable one as empty.

    Empty config means allowed_cwd_roots is missing, which fails closed in
    load_settings — the safe direction if config cannot be read.
    """
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def _found_on_path(binary: str) -> bool:
    return shutil.which(binary) is not None
