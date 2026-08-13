"""Run one acpx delegation and collect its output.

Owns the whole subprocess lifetime: spawn, stream, deadline, teardown. The
deadline here is the only thing stopping a wedged worker from holding a Slack
turn open indefinitely — but only while ``acp_delegate`` stays OUT of
``agent/tool_dispatch_helpers._PARALLEL_SAFE_TOOLS``. Tools in that set run on
the concurrent path, which abandons the whole batch after
``_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S`` (420 s) — shorter than this plugin's 900 s
default and its 3600 s maximum. Adding this tool there without also clamping the
timeout would report a live delegation as failed and leave the overlay installed
on a thread nobody is waiting for. Progress reporting would stop too, silently:
the status callback is registered in ``_begin_tool_execution``, which only the
sequential path reaches.

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
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from plugins.acp_delegation import parse

STDERR_TAIL_LINES = 40
POLL_INTERVAL_SECONDS = 0.1
TERMINATE_GRACE_SECONDS = 2

# How often the worker's current action is reported to the host. The host
# rate-limits its own persistence, so this only needs to be often enough that a
# reader sees movement, and rare enough that a chatty worker cannot turn the
# status line into a flicker.
ACTIVITY_INTERVAL_SECONDS = 5

# How often the host's activity clock is told the delegation is still alive.
# The gateway warns at 900 s of inactivity and abandons the turn at 1800 s
# (agent.gateway_timeout), so this leaves a wide margin for a worker that goes
# quiet between lines.
KEEP_ALIVE_INTERVAL_SECONDS = 60

# What the clock is told before the worker names its first action. Spawn and
# session setup produce output well before any tool call.
UNNAMED_ACTIVITY = "working"

# Readability, not a platform limit. The phrase lands on Slack's composer-footer
# status line, which accepted 300 characters when measured against the live API
# on 2026-08-13.
#
# Slack DOES enforce a hard 50 on its other status surface — `loading_messages`,
# the inline one — and rejects the whole call at 51 rather than truncating. That
# surface is deliberately not used: the upstream adapter never sends it, and
# upstream documents the inline text as Slack's own. Reaching for it meant
# patching a fourth core file, and every core file patched is a rebase conflict
# forever.
STATUS_PHRASE_MAX_CHARS = 160

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


@dataclass(frozen=True)
class RunRequest:
    """Everything one delegation needs in order to start.

    A parameter object rather than a flat argument list, which this module grew
    another entry on every time it learned something. The caller already holds
    these as a settings object plus a validated request.
    """

    acpx_bin: str
    worker: str
    task: str
    working_directory: str
    timeout_seconds: int
    kind_policy: Dict[str, Any]
    grace_seconds: int
    lease_id: str
    permission_mode: str = "auto"


class HostProgress(NamedTuple):
    """The host surfaces a running delegation may report to. Both optional.

    Injected rather than looked up here. Both callbacks are thread-local to the
    handler's thread, and ``tools.py`` is the only module in this plugin that
    imports Hermes — so the capture belongs there and the plumbing belongs here.
    """

    publish_status: Optional[Callable[[str], None]] = None
    report_activity: Optional[Callable[[str], None]] = None


def run(request: RunRequest, host_progress: Optional[HostProgress] = None) -> RunOutcome:
    """Execute one delegation and return everything observed.

    ``exit_code`` is None when the deadline fired and acpx had to be killed,
    which the caller reports differently from acpx timing the worker out itself.

    ``host_progress`` is absent in the tests and on any host that registered no
    callbacks. Reporting is a courtesy; the delegation runs identically without.
    """
    # A named session, not a one-shot `exec`. The mode can only be set on a
    # session, and the mode is what decides whether the worker may run anything
    # at all — see _open_session.
    _open_session(request)
    command = _build_command(request)
    process = _spawn(command, request.working_directory)

    lease_path = _write_lease(
        request.lease_id, process.pid, request.worker, request.working_directory
    )
    stdout_lines: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_tail: deque = deque(maxlen=STDERR_TAIL_LINES)
    # Readers outlive this function on the deadline path: _join gives up after a
    # second while the reader is still blocked on a pipe that only closes below.
    # Without this it can then paint a dead worker's phrase over the next tool's
    # status line.
    finished = threading.Event()

    try:
        # Readers first, and the prompt written from its own thread. Writing it
        # inline deadlocks on a task larger than the pipe buffer: this side
        # blocks in write() while acpx blocks writing output nobody is draining.
        # That happens before the deadline loop below is armed, so nothing would
        # ever time it out.
        _announce_waiting(request.worker, host_progress)
        readers = _start_readers(
            process,
            stdout_lines,
            stderr_tail,
            _progress_relay(request.worker, host_progress, finished),
        )
        writer = _start_writer(process, request.task)
        transcript, exit_code = _collect(
            process, stdout_lines, request.timeout_seconds + request.grace_seconds
        )
        _join(readers + [writer])
        return RunOutcome(transcript, exit_code, _stderr_text(stderr_tail))
    finally:
        finished.set()
        _terminate(process)
        _close_pipes(process)
        _remove_lease(lease_path)
        _close_session(request)


def lease_directory() -> str:
    """Where lease files live.

    Under HERMES_HOME/runtime, never inside the plugin directory: a deploy
    rsyncs the plugin tree with --delete and would erase an in-flight lease.
    """
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, LEASE_DIRECTORY)


CONTROL_TIMEOUT_SECONDS = 60


def _session_name(request: RunRequest) -> str:
    """One acpx session per run, named after it.

    Shares the run id with the lease and the settings overlay, so the three
    artefacts a delegation leaves behind can be traced to each other.
    """
    return "acp-{}".format(request.lease_id)


def _build_command(request: RunRequest) -> List[str]:
    """Assemble the prompt invocation.

    The task itself is not here. It goes over stdin via ``-f -`` so that no
    prompt can be mangled by argv quoting or hit an argument-length limit.
    """
    return [
        request.acpx_bin,
        "--cwd",
        request.working_directory,
        "--format",
        "json",
        "--timeout",
        str(request.timeout_seconds),
        "--permission-policy",
        json.dumps(request.kind_policy),
        request.worker,
        "-s",
        _session_name(request),
        "prompt",
        "-f",
        "-",
    ]


def _control(request: RunRequest, arguments: List[str], failure: str) -> None:
    """Run a short acpx control command, raising SpawnError if it fails."""
    command = [request.acpx_bin, "--cwd", request.working_directory] + arguments
    try:
        completed = subprocess.run(
            command,
            cwd=request.working_directory,
            capture_output=True,
            text=True,
            timeout=CONTROL_TIMEOUT_SECONDS,
            env=_build_environment(),
        )
    except FileNotFoundError:
        raise SpawnError(
            "acpx was not found at '{}'. Install it with `npm install -g acpx`, or set "
            "plugins.entries.acp_delegation.acpx_bin to its absolute path.".format(
                request.acpx_bin
            ),
            "acpx_not_found",
        )
    except subprocess.TimeoutExpired:
        raise SpawnError("{}: timed out.".format(failure), "spawn_failed")
    except OSError as error:
        raise SpawnError("{}: {}".format(failure, error), "spawn_failed")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SpawnError(
            "{} (exit {}). {}".format(failure, completed.returncode, detail[:500]),
            "spawn_failed",
        )


def _open_session(request: RunRequest) -> None:
    """Create the session and set the mode the worker runs under.

    Both are needed before the prompt, and the mode is the point. acpx sets none
    — its bundle contains no mode ids at all — so the worker starts in the
    adapter's ``default`` mode, asks about every action, and acpx answers from
    ``--permission-policy``. That policy matches tool KINDS and never paths, so
    it cannot allow "run git" without also allowing "run anything"; denying
    ``execute`` is what left a review worker unable to run ``git status``.

    ``auto`` moves the judgement back into the worker, which decides per action
    and mostly never asks — leaving the kind policy as the backstop for what it
    does escalate, rather than the first and only word.

    A failure here raises rather than warns. Continuing in the wrong mode
    reproduces the original fault exactly: a worker that cannot act, reporting
    something else as the reason.
    """
    session = _session_name(request)
    _control(
        request,
        [request.worker, "sessions", "new", "--name", session],
        "acpx could not create a session for the worker",
    )
    if not request.permission_mode:
        return
    _control(
        request,
        [request.worker, "-s", session, "set-mode", request.permission_mode],
        "acpx could not set the worker's mode to '{}'".format(request.permission_mode),
    )


def _close_session(request: RunRequest) -> None:
    """Best-effort teardown. A leaked session costs a stale record, not a run.

    ``sessions close`` takes the name POSITIONALLY. Passing it with ``-s`` looks
    right, is accepted, and closes nothing — it reports "No cwd session".
    """
    try:
        subprocess.run(
            [
                request.acpx_bin,
                "--cwd",
                request.working_directory,
                request.worker,
                "sessions",
                "close",
                _session_name(request),
            ],
            cwd=request.working_directory,
            capture_output=True,
            text=True,
            timeout=CONTROL_TIMEOUT_SECONDS,
            env=_build_environment(),
        )
    except Exception:
        pass


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

    Truncated rather than trusted: ``activity`` is a worker-supplied string read
    straight off the wire, so nothing bounds it but this.
    """
    phrase = "{} worker: {}".format(worker, activity)
    if len(phrase) <= STATUS_PHRASE_MAX_CHARS:
        return phrase
    return phrase[: STATUS_PHRASE_MAX_CHARS - 1] + "…"


def _announce_waiting(worker: str, host_progress: Optional[HostProgress]) -> None:
    """Say the worker is starting, before it has done anything worth reporting."""
    if host_progress is None or host_progress.publish_status is None:
        return
    _safely(host_progress.publish_status, waiting_phrase(worker))


def _progress_relay(
    worker: str, host_progress: Optional[HostProgress], finished: threading.Event
):
    """What the stdout reader drives, or None when no host is listening."""
    if host_progress is None:
        return None
    report_status = _surface(worker, host_progress.publish_status, finished)
    keep_alive = _surface(worker, host_progress.report_activity, finished)
    if report_status is None and keep_alive is None:
        return None
    return _ProgressRelay(report_status, keep_alive)


def _surface(
    worker: str, callback: Optional[Callable[[str], None]], finished: threading.Event
):
    """Wrap one host callback: it takes an activity, says the shared sentence,
    stays quiet once the run is over, and never raises.

    Both surfaces say the same thing, so the phrase is built in one place. What
    differs is when each is called, and that belongs to ``_ProgressRelay``.

    Nothing reports after ``finished`` is set. On the deadline path a reader
    outlives the run, and a late phrase paints a dead worker over whatever the
    host does next.
    """
    if callback is None:
        return None

    def send(activity: str) -> None:
        if finished.is_set():
            return
        _safely(callback, activity_phrase(worker, activity))

    return send


def _safely(surface: Callable[[str], None], phrase: str) -> None:
    """Hand a phrase to a host surface, absorbing whatever it does with it.

    Progress is a courtesy, and this runs on the only thread draining the
    worker's stdout: an exception escaping here stops that drain and the worker
    then blocks forever on a full pipe — a hang caused entirely by the code that
    exists to prove there is none.
    """
    try:
        surface(phrase)
    except Exception:
        pass


def _start_readers(
    process: subprocess.Popen,
    stdout_lines: "queue.Queue[Optional[str]]",
    stderr_tail: deque,
    relay=None,
) -> List[threading.Thread]:
    """Drain both pipes concurrently.

    Reading only one would deadlock: a full stderr buffer blocks the worker
    while this side waits on stdout.
    """
    readers = [
        threading.Thread(
            target=_read_stdout,
            args=(process, stdout_lines, relay),
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
    relay=None,
) -> None:
    """Drain stdout, and always post the sentinel.

    A reader that dies without posting it would leave the collector waiting for
    output that can never arrive, which reads as a hung worker rather than a
    broken pipe.

    Progress is derived here rather than in the collector because this thread
    sees every line as it arrives. The collector is free to fall behind, and a
    status report that lags the worker is worse than none.
    """
    try:
        for line in process.stdout:
            lines.put(line)
            _relay_line(relay, line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _relay_line(relay, line: str) -> None:
    """Offer one line to the relay, absorbing anything it does with it.

    This is the only thread draining the worker's stdout. An exception escaping
    here stops that drain, the worker then blocks forever on a full pipe, and
    the run hangs — caused entirely by the code that exists to prove it has not.
    Progress is never worth that, whether the fault is a host callback or a line
    shaped in a way the parser did not expect.
    """
    if relay is None:
        return
    try:
        relay.consider(line)
    except Exception:
        pass


class _Throttle:
    """Lets an event through at most once per interval."""

    def __init__(self, interval_seconds: float):
        self._interval = interval_seconds
        self._next_at = 0.0

    def due(self, now: float) -> bool:
        if now < self._next_at:
            return False
        self._next_at = now + self._interval
        return True


class _ProgressRelay:
    """Turns the worker's stdout into the two signals a host needs.

    Same sentence, deliberately different cadence:

    - The **status line** is read by a human, so it moves only when the work
      does. Repeating a phrase every few seconds is flicker, not information.
    - The **activity clock** proves the turn is alive to the gateway's
      inactivity watchdog (warns at 15 min, kills at 30). A repeat still counts.
      Reporting only on change is what let a live 26-minute delegation be warned
      about at 23: the worker had settled into one long step, so the title
      stopped changing and the clock went stale while it worked.

    Driven by the worker's own output, never by a bare timer. A timer would tick
    the clock forever and mask a genuinely hung worker, which is the fault the
    watchdog exists to catch — so a silent acpx still times out, as it should.

    Reads each line on its own rather than folding a transcript here: this
    thread needs one string, the collector already owns the real transcript, and
    a second copy would retain a whole 90-minute run to read the newest line off
    the end of it.
    """

    def __init__(self, report_status=None, keep_alive=None):
        self._report_status = report_status
        self._keep_alive = keep_alive
        self._status_due = _Throttle(ACTIVITY_INTERVAL_SECONDS)
        self._keep_alive_due = _Throttle(KEEP_ALIVE_INTERVAL_SECONDS)
        self._last_reported = None
        self._latest_activity = None

    def consider(self, line: str) -> None:
        now = time.monotonic()
        activity = parse.activity_from_line(line)
        if activity:
            self._latest_activity = activity
            self._show_status(activity, now)
        # Any line at all is evidence the worker is alive, including one this
        # plugin has no opinion about.
        self._keep_clock_warm(now)

    def _show_status(self, activity: str, now: float) -> None:
        if self._report_status is None or activity == self._last_reported:
            return
        if not self._status_due.due(now):
            return
        self._last_reported = activity
        self._report_status(activity)

    def _keep_clock_warm(self, now: float) -> None:
        if self._keep_alive is None or not self._keep_alive_due.due(now):
            return
        self._keep_alive(self._latest_activity or UNNAMED_ACTIVITY)


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
