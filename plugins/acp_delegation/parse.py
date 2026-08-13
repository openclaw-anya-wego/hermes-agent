"""Turn an ``acpx --format json`` stdout stream into a delegation result.

This module is deliberately pure: it imports nothing from Hermes and touches no
subprocess, filesystem, or clock. Everything it needs arrives as arguments, so
the whole parsing contract — including the false-success guard that is the point
of the plugin — is testable with fixtures on any machine.

The wire format is raw JSON-RPC NDJSON. The published acpx documentation shows
normalised events carrying a ``type`` field; the CLI does not emit those. Shapes
here were captured from acpx 0.13.0 driving claude-agent-acp 0.60.0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TRUNCATION_NOTICE = "\n\n… [truncated, kept {kept} of {total} chars]"

# acpx exit codes. Documented at https://acpx.sh/exit-codes.html and confirmed
# against the CLI: a fully-denied permission run really does exit 5.
EXIT_OK = 0
EXIT_AGENT_ERROR = 1
EXIT_CLI_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_NO_SESSION = 4
EXIT_PERMISSION_DENIED = 5
EXIT_INTERRUPTED = 130

_EXIT_ERROR_TYPES = {
    EXIT_AGENT_ERROR: "agent_error",
    EXIT_CLI_USAGE: "cli_usage_error",
    EXIT_TIMEOUT: "timeout",
    EXIT_NO_SESSION: "no_session",
    EXIT_PERMISSION_DENIED: "permission_denied",
    EXIT_INTERRUPTED: "interrupted",
}

_EXIT_MESSAGES = {
    EXIT_AGENT_ERROR: "The worker reported an agent, protocol, or runtime error.",
    EXIT_CLI_USAGE: "acpx rejected the arguments this plugin built. This is a plugin bug.",
    EXIT_TIMEOUT: "The worker exceeded the acpx timeout and was stopped by acpx.",
    EXIT_NO_SESSION: "acpx found no session to prompt.",
    EXIT_PERMISSION_DENIED: "Every action the worker attempted was denied by the permission policy.",
    EXIT_INTERRUPTED: "The worker was interrupted before it finished.",
}


@dataclass
class PermissionRequest:
    """One action the worker asked permission for, as reported on the wire."""

    kind: str
    title: str
    file_path: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "file_path": self.file_path}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_write_tokens: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_write_tokens": self.cached_write_tokens,
        }


@dataclass
class Transcript:
    """Everything worth keeping from one acpx run."""

    text_fragments: List[str] = field(default_factory=list)
    permission_requests: List[PermissionRequest] = field(default_factory=list)
    usage: Optional[Usage] = None
    stop_reason: Optional[str] = None
    cost_amount: Optional[float] = None
    cost_currency: Optional[str] = None
    saw_final_result: bool = False
    # Slash commands the worker advertised. Empty means it never said — which is
    # not the same as "it has none", so callers must fail open on an empty list.
    available_commands: List[str] = field(default_factory=list)
    # The worker's most recent action, for progress reporting. A delegation
    # blocks for up to 90 minutes, so without this the host has no evidence the
    # run is alive and cannot tell a working worker from a hung one.
    last_activity: Optional[str] = None

    @property
    def text(self) -> str:
        """The worker's reply.

        Text arrives as fragments — a one-word answer streamed as "B" then
        "ANANA" — so order of arrival is the only thing that reconstructs it.
        """
        return "".join(self.text_fragments)


def consume_line(transcript: Transcript, raw_line: str) -> None:
    """Fold one NDJSON line into the transcript.

    Unparseable and unrecognised lines are ignored on purpose. acpx interleaves
    protocol chatter that this plugin has no opinion about, and a stream that
    gains a new message type should not fail a delegation.
    """
    message = _load_json(raw_line)
    if message is None:
        return

    method = message.get("method")
    if method == "session/update":
        _consume_session_update(transcript, message.get("params", {}).get("update", {}))
        return
    if method == "session/request_permission":
        _consume_permission_request(transcript, message.get("params", {}))
        return
    if method is None:
        _consume_result(transcript, message.get("result"))


def build_result(
    transcript: Transcript,
    exit_code: Optional[int],
    max_response_chars: int,
    stderr_tail: str = "",
) -> Dict[str, Any]:
    """Decide whether the run succeeded and shape the payload for the model.

    ``exit_code`` is None when this plugin killed acpx on its own deadline,
    which is a different fault from acpx reporting its own timeout: one means
    the worker overran, the other means acpx itself stopped responding.
    """
    if exit_code is None:
        return _failure(
            "acpx did not exit before the plugin deadline and was killed.",
            "plugin_deadline_exceeded",
            transcript,
            exit_code,
            max_response_chars,
            stderr_tail,
        )

    if exit_code != EXIT_OK:
        return _failure(
            _EXIT_MESSAGES.get(exit_code, "acpx exited with code {}.".format(exit_code)),
            _EXIT_ERROR_TYPES.get(exit_code, "unknown_exit"),
            transcript,
            exit_code,
            max_response_chars,
            stderr_tail,
        )

    if not transcript.saw_final_result:
        return _failure(
            "acpx exited cleanly but never reported a result. The output cannot be trusted.",
            "malformed_output",
            transcript,
            exit_code,
            max_response_chars,
            stderr_tail,
        )

    if _is_false_success(transcript):
        return _failure(
            "The worker reported completion without producing any output. Nothing was done.",
            "false_success",
            transcript,
            exit_code,
            max_response_chars,
            stderr_tail,
        )

    response, truncated = _truncate(transcript.text, max_response_chars)
    return {
        "success": True,
        "stop_reason": transcript.stop_reason,
        "response": response,
        "response_truncated": truncated,
        "usage": (transcript.usage or Usage()).as_dict(),
        "cost": _cost(transcript),
        "permission_requests": [r.as_dict() for r in transcript.permission_requests],
        "exit_code": exit_code,
    }


def _is_false_success(transcript: Transcript) -> bool:
    """True when the worker claimed to finish but demonstrably did nothing.

    Both signals are required because either alone gives a false positive: a
    worker can legitimately act without narrating, and it can legitimately
    answer without editing.

    The token side must read ``output_tokens``. ``total_tokens`` is useless as a
    guard — a one-word reply measured 31,206 total tokens because 31,198 of them
    were cache writes, so a total-token check can never fire.
    """
    produced_text = bool(transcript.text.strip())
    produced_tokens = bool(transcript.usage and transcript.usage.output_tokens > 0)
    return not produced_text and not produced_tokens


def _failure(
    message: str,
    error_type: str,
    transcript: Transcript,
    exit_code: Optional[int],
    max_response_chars: int,
    stderr_tail: str,
) -> Dict[str, Any]:
    partial, truncated = _truncate(transcript.text, max_response_chars)
    return {
        "success": False,
        "error": message,
        "error_type": error_type,
        "exit_code": exit_code,
        "partial_response": partial,
        "partial_response_truncated": truncated,
        "permission_requests": [r.as_dict() for r in transcript.permission_requests],
        "stderr_tail": stderr_tail,
    }


def _truncate(text: str, max_chars: int) -> tuple:
    """Last-resort bound on a runaway reply.

    Not the primary mechanism. Hermes already spills an oversized tool result to
    a file and hands the model a readable path (``maybe_persist_tool_result``),
    which preserves the whole reply instead of discarding its tail — so the
    plugin declares ``max_result_size_chars`` at registration and lets the host
    do it. This only fires if a worker returns something larger still.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    notice = TRUNCATION_NOTICE.format(kept=max_chars, total=len(text))
    return text[:max_chars] + notice, True


def _cost(transcript: Transcript) -> Optional[Dict[str, Any]]:
    if transcript.cost_amount is None:
        return None
    return {"amount": transcript.cost_amount, "currency": transcript.cost_currency or "USD"}


def _load_json(raw_line: str) -> Optional[Dict[str, Any]]:
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        message = json.loads(stripped)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _consume_session_update(transcript: Transcript, update: Dict[str, Any]) -> None:
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = update.get("content", {}).get("text")
        if text:
            transcript.text_fragments.append(text)
        return
    if kind == "usage_update":
        _consume_cost(transcript, update.get("cost"))
        return
    if kind == "available_commands_update":
        _consume_available_commands(transcript, update.get("availableCommands"))
        return
    if kind in ("tool_call", "tool_call_update"):
        _consume_activity(transcript, update)


def _consume_activity(transcript: Transcript, update: Dict[str, Any]) -> None:
    """Record what the worker is doing, in its own words.

    The adapter's ``title`` is already written for a human ("Read
    FareParser.java", "Run bun test") — better than anything this module could
    synthesise from the raw tool name and arguments, and it is the same field an
    editor would display.

    Only ``in_progress`` and untagged calls count. A ``completed`` update would
    report the previous action as the current one for however long the next step
    takes, which reads as a stall precisely when the worker is busiest.
    """
    if update.get("status") == "completed":
        return
    title = update.get("title")
    if isinstance(title, str) and title.strip():
        transcript.last_activity = title.strip()


def _consume_available_commands(transcript: Transcript, commands: Any) -> None:
    """Replace the command list rather than extending it.

    The adapter pushes the *full* list whenever it changes, so appending would
    accumulate duplicates and keep names that a mid-session change removed.
    """
    if not isinstance(commands, list):
        return
    transcript.available_commands = [
        command["name"]
        for command in commands
        if isinstance(command, dict) and isinstance(command.get("name"), str)
    ]


def _consume_cost(transcript: Transcript, cost: Optional[Dict[str, Any]]) -> None:
    """Keep the latest cost. Usage events are cumulative, and only some carry it."""
    if not isinstance(cost, dict):
        return
    amount = cost.get("amount")
    if amount is None:
        return
    transcript.cost_amount = amount
    transcript.cost_currency = cost.get("currency")


def _consume_permission_request(transcript: Transcript, params: Dict[str, Any]) -> None:
    tool_call = params.get("toolCall", {})
    transcript.permission_requests.append(
        PermissionRequest(
            kind=tool_call.get("kind", "unknown"),
            title=tool_call.get("title", ""),
            file_path=tool_call.get("rawInput", {}).get("file_path"),
        )
    )


def _consume_result(transcript: Transcript, result: Optional[Dict[str, Any]]) -> None:
    """Record the reply to session/prompt.

    Handshake and session/new replies are results too, so the stop_reason field
    is what identifies the one that ends the turn.
    """
    if not isinstance(result, dict) or "stopReason" not in result:
        return
    transcript.saw_final_result = True
    transcript.stop_reason = result.get("stopReason")
    transcript.usage = _read_usage(result.get("usage"))


def _read_usage(usage: Optional[Dict[str, Any]]) -> Usage:
    if not isinstance(usage, dict):
        return Usage()
    return Usage(
        input_tokens=usage.get("inputTokens", 0) or 0,
        output_tokens=usage.get("outputTokens", 0) or 0,
        total_tokens=usage.get("totalTokens", 0) or 0,
        cached_write_tokens=usage.get("cachedWriteTokens", 0) or 0,
    )
