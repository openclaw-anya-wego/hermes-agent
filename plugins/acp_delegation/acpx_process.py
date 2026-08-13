"""Run one acpx delegation and collect its output.

Owns the whole subprocess lifetime: spawn, stream, deadline, teardown. The
deadline here is the only thing stopping a wedged worker from holding a Slack
turn open indefinitely — but only while ``acp_delegate`` stays OUT of
``agent/tool_dispatch_helpers._PARALLEL_SAFE_TOOLS``. Tools in that set run on
the concurrent path, which abandons the whole batch after
``_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S`` (420 s) — shorter than this plugin's 900 s
default and its 3600 s maximum. Adding this tool there without also clamping the
timeout would report a live delegation as failed and leave the overlay installed
on a thread nobody is waiting for.

While a worker is running this writes a lease file that ``safe-restart.sh``
reads, so a gateway restart can tell a delegation is in flight. The lease is
deliberately keyed on a pid the plugin itself records rather than on process
names: whether the worker appears as ``claude`` or as ``node`` depends on how npm
resolved the bundled adapter, and that has silently changed under the existing
restart guard before.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from plugins.acp_delegation import parse

STDERR_TAIL_LINES = 40
POLL_INTERVAL_SECONDS = 0.1
TERMINATE_GRACE_SECONDS = 2

# How often the worker's current action is reported to the host. The host
# rate-limits its own persistence, so this only needs to be often enough that a
# reader sees movement, and rare enough that a chatty worker cannot turn the
# status line into a flicker.
ACTIVITY_INTERVAL_SECONDS = 5

LEASE_DIRECTORY = os.path.join("runtime", "acp_delegation", "active")


class SpawnError(Exception):
    """acpx could not be started at all."""

    def __init__(self, message: str, error_type: str):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class RunOutcome:
    transcript: parse.Transcript
    exit_code: Optional[int]
    stderr_tail: str


def run(
    acpx_bin: str,
    worker: str,
    task: str,
    working_directory: str,
    timeout_seconds: int,
    kind_policy: Dict[str, Any],
    grace_seconds: int,
    lease_id: str,
) -> RunOutcome:
    """Execute one delegation and return everything observed.

    ``exit_code`` is None when the deadline fired and acpx had to be killed,
    which the caller reports differently from acpx timing the worker out itself.
    """
    command = _build_command(acpx_bin, worker, working_directory, timeout_seconds, kind_policy)
    process = _spawn(command, working_directory)

    lease_path = _write_lease(lease_id, process.pid, worker, working_directory)
    stdout_lines: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_tail: deque = deque(maxlen=STDERR_TAIL_LINES)

    try:
        # Readers first, and the prompt written from its own thread. Writing it
        # inline deadlocks on a task larger than the pipe buffer: this side
        # blocks in write() while acpx blocks writing output nobody is draining.
        # That happens before the deadline loop below is armed, so nothing would
        # ever time it out.
        # Captured here, on the handler's thread. The host's activity callback
        # is thread-local, so a reader thread cannot look it up for itself —
        # get_activity_callback exists for exactly this handoff.
        publish_status = _capture_status_publisher()
        if publish_status is not None:
            publish_status(waiting_phrase(worker))
        readers = _start_readers(
            process,
            stdout_lines,
            stderr_tail,
            _combined_reporter(worker, publish_status),
        )
        writer = _start_writer(process, task)
        transcript, exit_code = _collect(
            process, stdout_lines, timeout_seconds + grace_seconds
        )
        _join(readers + [writer])
        return RunOutcome(transcript, exit_code, _stderr_text(stderr_tail))
    finally:
        _terminate(process)
        _close_pipes(process)
        _remove_lease(lease_path)


def lease_directory() -> str:
    """Where lease files live.

    Under HERMES_HOME/runtime, never inside the plugin directory: a deploy
    rsyncs the plugin tree with --delete and would erase an in-flight lease.
    """
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, LEASE_DIRECTORY)


def _build_command(
    acpx_bin: str,
    worker: str,
    working_directory: str,
    timeout_seconds: int,
    kind_policy: Dict[str, Any],
) -> List[str]:
    """Assemble the acpx invocation.

    The task itself is not here. It goes over stdin via ``-f -`` so that no
    prompt can be mangled by argv quoting or hit an argument-length limit.
    """
    return [
        acpx_bin,
        "--cwd",
        working_directory,
        "--format",
        "json",
        "--timeout",
        str(timeout_seconds),
        "--permission-policy",
        json.dumps(kind_policy),
        worker,
        "exec",
        "-f",
        "-",
    ]


def _spawn(command: List[str], working_directory: str) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            command,
            cwd=working_directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # A worker can relay a non-UTF-8 byte from a file it read. Strict
            # decoding would kill the reader thread silently, and the run would
            # then look like a hang rather than an encoding fault.
            errors="replace",
            bufsize=1,
            env=_build_environment(),
        )
    except FileNotFoundError:
        raise SpawnError(
            "acpx was not found at '{}'. Install it with `npm install -g acpx`, or set "
            "plugins.entries.acp_delegation.acpx_bin to its absolute path.".format(command[0]),
            "acpx_not_found",
        )
    except OSError as error:
        raise SpawnError("Could not start acpx: {}".format(error), "spawn_failed")


def _build_environment() -> Dict[str, str]:
    """The worker's environment: ours, plus the user-scope opt-in.

    acpx defaults Claude Code's setting sources to ``["project", "local"]`` and
    drops ``user`` unless ``ACPX_CLAUDE_INCLUDE_USER_SETTINGS=1``. That default
    silently removes everything under ``~/.claude`` — including
    ``~/.claude/commands``, which is where the operator's delegation commands
    live. The symptom is not an error: the worker answers ``Unknown command:
    /saber-code-review`` at exit 0, having improvised on the brief instead.

    Anya's path does not hit this because OpenClaw's acpx *extension* drives the
    adapter directly and gets its default of ``["user", "project", "local"]``.
    Reaching the same worker through the acpx CLI does not.
    """
    environment = os.environ.copy()
    environment["ACPX_CLAUDE_INCLUDE_USER_SETTINGS"] = "1"
    return environment


def _start_writer(process: subprocess.Popen, task: str) -> threading.Thread:
    """Send the prompt on its own thread, so a full pipe cannot block the caller."""
    writer = threading.Thread(target=_send_task, args=(process, task), daemon=True)
    writer.start()
    return writer


def _send_task(process: subprocess.Popen, task: str) -> None:
    """Hand the prompt over and close stdin so acpx stops waiting for more."""
    try:
        process.stdin.write(task)
        process.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        # acpx died before reading the prompt. The exit code explains why, so
        # let the collection path report it rather than raising a second fault
        # from a daemon thread nobody is watching.
        pass


def _stderr_text(tail: deque) -> str:
    """Snapshot stderr without iterating a deque another thread may append to.

    ``list()`` on a deque is atomic under the GIL; iterating one lazily while a
    reader thread appends raises RuntimeError, which would escape the handler's
    must-not-raise contract.
    """
    return "".join(list(tail)).strip()


def waiting_phrase(worker: str) -> str:
    """What the operator sees before the worker has done anything.

    Spawn and session setup take 5-15 seconds, and the worker's first action
    can be a minute out on a large checkout. Without this the status line still
    reads as the generic tool name for that whole stretch.
    """
    return "Delegating task to {} worker…".format(worker)


def activity_phrase(worker: str, activity: str) -> str:
    """What the worker is doing, in the operator's status line.

    The worker name is interpolated, never branched on, so a new worker needs no
    change here — it only has to speak ACP, which is the one thing every worker
    this plugin can reach already does.

    Kept short on purpose: the platform truncates around 50 characters, and
    ``<worker> worker: `` already spends up to 15 of them.
    """
    return "{} worker: {}".format(worker, activity)


def _capture_status_publisher():
    """A callable that sets the operator-visible status phrase, or None.

    Takes a finished phrase, so the caller owns the wording — the waiting text
    and the per-action text are different sentences, not one template.

    Captured on the handler's thread: the host's callback is thread-local and a
    reader thread cannot look it up for itself.
    """
    try:
        from tools.environments.base import get_status_callback

        callback = get_status_callback()
    except Exception:
        return None
    return callback


def _combined_reporter(worker: str, publish_status):
    """One reporter driving both surfaces, or None when neither exists.

    They want the same events at the same rate but say different things: the
    status line is a sentence for a human, the activity clock is a heartbeat
    that keeps the stall watchdog quiet. Throttling them together keeps the two
    from disagreeing about what the worker is doing.
    """
    report_activity = _capture_activity_reporter(worker)
    if report_activity is None and publish_status is None:
        return None

    def report(activity: str) -> None:
        if publish_status is not None:
            try:
                publish_status(activity_phrase(worker, activity))
            except Exception:
                pass
        if report_activity is not None:
            report_activity(activity)

    return report


def _capture_activity_reporter(worker: str):
    """A callable that tells the host what the worker is doing, or None.

    Returns None when Hermes is absent — the tests run without it — or when the
    host registered no callback, so progress reporting stays entirely optional.

    Reporting matters more here than for an ordinary tool. This call blocks for
    as long as the delegation runs, up to 90 minutes, and a host that sees no
    activity for that long cannot distinguish a working worker from a hung one.
    """
    try:
        from tools.environments.base import get_activity_callback

        callback = get_activity_callback()
    except Exception:
        return None
    if callback is None:
        return None

    def report(activity: str) -> None:
        try:
            callback("{} worker: {}".format(worker, activity))
        except Exception:
            # Progress is a courtesy. A host that raises here must not take the
            # delegation down with it.
            pass

    return report


def _start_readers(
    process: subprocess.Popen,
    stdout_lines: "queue.Queue[Optional[str]]",
    stderr_tail: deque,
    report_activity=None,
) -> List[threading.Thread]:
    """Drain both pipes concurrently.

    Reading only one would deadlock: a full stderr buffer blocks the worker
    while this side waits on stdout.
    """
    readers = [
        threading.Thread(
            target=_read_stdout,
            args=(process, stdout_lines, report_activity),
            daemon=True,
        ),
        threading.Thread(target=_read_stderr, args=(process, stderr_tail), daemon=True),
    ]
    for reader in readers:
        reader.start()
    return readers


def _read_stdout(
    process: subprocess.Popen,
    lines: "queue.Queue[Optional[str]]",
    report_activity=None,
) -> None:
    """Drain stdout, and always post the sentinel.

    A reader that dies without posting it would leave the collector waiting for
    output that can never arrive, which reads as a hung worker rather than a
    broken pipe.

    Progress is derived here rather than in the collector because this thread
    sees every line as it arrives. The collector is free to fall behind, and a
    status report that lags the worker is worse than none.
    """
    reporter = _ActivityThrottle(report_activity)
    try:
        for line in process.stdout:
            lines.put(line)
            reporter.consider(line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


class _ActivityThrottle:
    """Reports the worker's current action, no more than one report per window.

    Keeps its own throwaway transcript: this thread only needs the newest
    activity line, and folding into the real transcript from here would race the
    collector that owns it.
    """

    def __init__(self, report):
        self._report = report
        self._transcript = parse.Transcript()
        self._last_reported = None
        self._next_report_at = 0.0

    def consider(self, line: str) -> None:
        if self._report is None:
            return
        parse.consume_line(self._transcript, line)
        activity = self._transcript.last_activity
        if not activity or activity == self._last_reported:
            return
        now = time.monotonic()
        if now < self._next_report_at:
            return
        self._last_reported = activity
        self._next_report_at = now + ACTIVITY_INTERVAL_SECONDS
        try:
            self._report(activity)
        except Exception:
            # This runs on the stdout reader. An exception escaping here kills
            # the only thread draining that pipe, and the worker then blocks
            # forever on a full buffer — a hang caused entirely by the code that
            # exists to prove there is no hang.
            pass


def _read_stderr(process: subprocess.Popen, tail: deque) -> None:
    try:
        for line in process.stderr:
            tail.append(line)
    except (OSError, ValueError):
        pass


def _collect(
    process: subprocess.Popen,
    stdout_lines: "queue.Queue[Optional[str]]",
    deadline_seconds: int,
) -> tuple:
    """Fold stdout into a transcript until the process ends or time runs out."""
    transcript = parse.Transcript()
    deadline = time.monotonic() + deadline_seconds
    stdout_finished = False

    while True:
        if time.monotonic() > deadline:
            return transcript, None

        try:
            line = stdout_lines.get(timeout=POLL_INTERVAL_SECONDS)
        except queue.Empty:
            if stdout_finished and process.poll() is not None:
                return transcript, process.returncode
            continue

        if line is None:
            stdout_finished = True
            continue

        parse.consume_line(transcript, line)


def _join(readers: List[threading.Thread]) -> None:
    for reader in readers:
        reader.join(timeout=1)


def _terminate(process: subprocess.Popen) -> None:
    """Stop the process if it is still alive, escalating only if it ignores us."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _close_pipes(process: subprocess.Popen) -> None:
    """Release the pipe file objects.

    The gateway is long-lived, so leaking three descriptors per delegation would
    eventually reach the plist's NumberOfFiles limit.
    """
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _write_lease(lease_id: str, pid: int, worker: str, working_directory: str) -> Optional[str]:
    directory = lease_directory()
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "{}.json".format(lease_id))
        payload = {
            "pid": pid,
            "worker": worker,
            "cwd": working_directory,
            "started_at": time.time(),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path
    except OSError:
        # A missing lease weakens the restart guard but must never fail the
        # delegation the operator actually asked for.
        return None


def _remove_lease(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
