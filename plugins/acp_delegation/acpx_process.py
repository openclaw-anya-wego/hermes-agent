"""Run one acpx delegation and collect its output.

Owns the whole subprocess lifetime: spawn, stream, deadline, teardown. Hermes
applies no timeout to a tool handler, so the deadline here is the only thing
stopping a wedged worker from holding a Slack turn open indefinitely.

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
        _send_task(process, task)
        readers = _start_readers(process, stdout_lines, stderr_tail)
        transcript, exit_code = _collect(
            process, stdout_lines, timeout_seconds + grace_seconds
        )
        _join(readers)
        return RunOutcome(transcript, exit_code, "".join(stderr_tail).strip())
    finally:
        _terminate(process)
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
            bufsize=1,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        raise SpawnError(
            "acpx was not found at '{}'. Install it with `npm install -g acpx`, or set "
            "plugins.entries.acp_delegation.acpx_bin to its absolute path.".format(command[0]),
            "acpx_not_found",
        )
    except OSError as error:
        raise SpawnError("Could not start acpx: {}".format(error), "spawn_failed")


def _send_task(process: subprocess.Popen, task: str) -> None:
    """Hand the prompt over and close stdin so acpx stops waiting for more."""
    try:
        process.stdin.write(task)
        process.stdin.close()
    except (BrokenPipeError, ValueError):
        # acpx died before reading the prompt. The exit code explains why, so
        # let the collection path report it rather than raising a second fault.
        pass


def _start_readers(
    process: subprocess.Popen,
    stdout_lines: "queue.Queue[Optional[str]]",
    stderr_tail: deque,
) -> List[threading.Thread]:
    """Drain both pipes concurrently.

    Reading only one would deadlock: a full stderr buffer blocks the worker
    while this side waits on stdout.
    """
    readers = [
        threading.Thread(target=_read_stdout, args=(process, stdout_lines), daemon=True),
        threading.Thread(target=_read_stderr, args=(process, stderr_tail), daemon=True),
    ]
    for reader in readers:
        reader.start()
    return readers


def _read_stdout(process: subprocess.Popen, lines: "queue.Queue[Optional[str]]") -> None:
    for line in process.stdout:
        lines.put(line)
    lines.put(None)


def _read_stderr(process: subprocess.Popen, tail: deque) -> None:
    for line in process.stderr:
        tail.append(line)


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
