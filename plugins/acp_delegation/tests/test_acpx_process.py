"""Tests for the subprocess lifecycle.

A small Python script stands in for acpx, so these run offline and still
exercise the real Popen, the real reader threads, and the real deadline.

Both bugs this module shipped with lived here and were invisible to the rest of
the suite, which stubs `run` out entirely.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, REPO_ROOT)

from plugins.acp_delegation import acpx_process  # noqa: E402

KIND_POLICY = {"autoApprove": ["read"], "defaultAction": "deny"}

# A stand-in for acpx. Reads the whole prompt from stdin, then emits a valid
# NDJSON stream. The read-everything-first order is what makes it a regression
# test for the stdin deadlock.
FAKE_WORKER = """
import json, sys, time
task = sys.stdin.read()
chatter = int(sys.argv[1]) if len(sys.argv) > 1 else 0
for i in range(chatter):
    print(json.dumps({"method": "session/update", "params": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"text": "x"}}}}), flush=True)
print(json.dumps({"method": "session/update", "params": {"update": {
    "sessionUpdate": "agent_message_chunk", "content": {"text": str(len(task))}}}}), flush=True)
print(json.dumps({"id": 2, "result": {"stopReason": "end_turn",
    "usage": {"outputTokens": 5, "totalTokens": 31206}}}), flush=True)
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
"""

SLOW_WORKER = """
import sys, time
sys.stdin.read()
time.sleep(60)
"""


class ProcessTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

        self.previous_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self.home
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.previous_home

    def fake_worker(self, source=FAKE_WORKER, chatter=0, exit_code=0):
        """Write a stand-in worker and return a shell wrapper acting as acpx.

        The wrapper swallows the acpx flags the plugin passes and forwards only
        what the stub needs, so the plugin's real argv is exercised.
        """
        script = os.path.join(self.workdir, "worker.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(source)

        # Control calls must be answered separately from the prompt. The plugin
        # opens a named session and sets its mode before prompting, and a stub
        # that ran the worker for those too would make a slow worker cost three
        # timeouts and a failing worker fail during setup instead of during the
        # run — neither of which is what the test is about.
        wrapper = os.path.join(self.workdir, "fake-acpx")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(
                '#!/bin/sh\n'
                'for arg in "$@"; do\n'
                '  case "$arg" in sessions|set-mode) exit 0 ;; esac\n'
                'done\n'
                'exec "{}" "{}" {} {}\n'.format(
                    sys.executable, script, chatter, exit_code
                )
            )
        os.chmod(wrapper, 0o755)
        return wrapper

    def run_task(
        self,
        binary,
        task="hello",
        timeout_seconds=30,
        grace_seconds=5,
        host_progress=None,
    ):
        return acpx_process.run(
            acpx_process.RunRequest(
                acpx_bin=binary,
                worker="claude",
                task=task,
                working_directory=self.workdir,
                timeout_seconds=timeout_seconds,
                kind_policy=KIND_POLICY,
                grace_seconds=grace_seconds,
                lease_id="testlease",
            ),
            host_progress,
        )


class HappyPathTests(ProcessTestCase):
    def test_collects_the_stream_and_the_exit_code(self):
        outcome = self.run_task(self.fake_worker(), task="hello")

        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.transcript.saw_final_result)
        self.assertEqual(outcome.transcript.usage.output_tokens, 5)

    def test_delivers_the_whole_task_on_stdin(self):
        """The stub echoes the prompt length, so a short read shows up here."""
        outcome = self.run_task(self.fake_worker(), task="x" * 5000)

        self.assertIn("5000", outcome.transcript.text)

    def test_delivers_a_task_larger_than_the_pipe_buffer(self):
        """Regression: writing stdin before starting the readers deadlocked.

        A prompt past the ~16 KiB pipe buffer blocks in write() while the worker
        blocks writing output nobody drains. The reader threads must be running
        first.
        """
        big = "y" * 300000

        outcome = self.run_task(self.fake_worker(chatter=200), task=big)

        self.assertEqual(outcome.exit_code, 0)
        self.assertIn(str(len(big)), outcome.transcript.text)

    def test_reports_a_nonzero_exit(self):
        outcome = self.run_task(self.fake_worker(exit_code=3))

        self.assertEqual(outcome.exit_code, 3)


class DeadlineTests(ProcessTestCase):
    def test_reports_no_exit_code_when_the_deadline_kills_the_worker(self):
        outcome = self.run_task(
            self.fake_worker(source=SLOW_WORKER), timeout_seconds=1, grace_seconds=0
        )

        self.assertIsNone(outcome.exit_code)

    def test_kills_the_worker_it_gave_up_on(self):
        self.run_task(self.fake_worker(source=SLOW_WORKER), timeout_seconds=1, grace_seconds=0)

        self.assertEqual(os.listdir(acpx_process.lease_directory()), [])


class SpawnFailureTests(ProcessTestCase):
    def test_explains_a_missing_binary_actionably(self):
        with self.assertRaises(acpx_process.SpawnError) as raised:
            self.run_task("/nonexistent/acpx")

        self.assertEqual(raised.exception.error_type, "acpx_not_found")
        self.assertIn("npm install -g acpx", str(raised.exception))

    def test_leaves_no_lease_behind_when_the_spawn_fails(self):
        with self.assertRaises(acpx_process.SpawnError):
            self.run_task("/nonexistent/acpx")

        self.assertFalse(os.path.isdir(acpx_process.lease_directory()))


class LeaseTests(ProcessTestCase):
    def test_writes_the_shape_safe_restart_parses(self):
        """safe-restart.sh greps "pid" out of this file with sed.

        The path and the key are literals in another repository, so this test is
        the only thing standing between a rename here and a restart guard that
        silently stops seeing delegated workers.
        """
        path = acpx_process._write_lease("abc123", 4242, "claude", "/tmp/repo")

        self.assertTrue(path.endswith(os.path.join("runtime", "acp_delegation", "active", "abc123.json")))
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pid"], 4242)

    def test_honours_hermes_home(self):
        self.assertTrue(acpx_process.lease_directory().startswith(self.home))

    def test_removes_the_lease_after_a_normal_run(self):
        self.run_task(self.fake_worker())

        self.assertEqual(os.listdir(acpx_process.lease_directory()), [])

    def test_a_failure_to_write_the_lease_does_not_fail_the_delegation(self):
        os.environ["HERMES_HOME"] = "/proc/nonexistent-and-unwritable"

        outcome = self.run_task(self.fake_worker())

        self.assertEqual(outcome.exit_code, 0)

    def test_leaves_no_staging_file_for_the_reader_to_trip_over(self):
        acpx_process._write_lease("abc123", 4242, "claude", "/tmp/repo")

        self.assertEqual(os.listdir(acpx_process.lease_directory()), ["abc123.json"])

    def test_a_write_that_dies_mid_flush_publishes_no_lease(self):
        """The reason the write is a rename rather than an in-place truncate.

        safe-restart.sh aborts on a lease it cannot parse, and nothing reaps one
        whose owner is gone — so a zero-byte file here blocks every future
        restart of the gateway, permanently and by hand-repair only.
        """
        directory = acpx_process.lease_directory()
        os.makedirs(directory, exist_ok=True)

        def die_mid_write(*_args, **_kwargs):
            raise OSError("killed between truncate and flush")

        with mock.patch.object(acpx_process.json, "dump", die_mid_write):
            path = acpx_process._write_lease("abc123", 4242, "claude", "/tmp/repo")

        self.assertIsNone(path)
        self.assertEqual(
            [name for name in os.listdir(directory) if name.endswith(".json")], []
        )


class StatusRelayTests(unittest.TestCase):
    """What the operator's status line shows, from the reader thread.

    Exercised directly rather than through a subprocess: the rate limiting and
    de-duplication live here, and both are invisible in an end-to-end run.
    """

    def setUp(self):
        self.reported = []
        self.throttle = acpx_process._ProgressRelay(report_status=self.reported.append)

    def line(self, title, status="in_progress"):
        return json.dumps(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "title": title,
                        "status": status,
                    }
                },
            }
        )

    def test_reports_the_first_action_immediately(self):
        self.throttle.consider(self.line("Read a.java"))

        self.assertEqual(self.reported, ["Read a.java"])

    def test_throttles_a_chatty_worker(self):
        """A worker doing ten things a second must not flicker the status."""
        for index in range(10):
            self.throttle.consider(self.line("Step {}".format(index)))

        self.assertEqual(self.reported, ["Step 0"])

    def test_reports_again_once_the_window_passes(self):
        self.throttle.consider(self.line("Read a.java"))
        self.throttle._status_due._next_at = 0.0

        self.throttle.consider(self.line("Edit b.java"))

        self.assertEqual(self.reported, ["Read a.java", "Edit b.java"])

    def test_does_not_repeat_an_unchanged_action(self):
        self.throttle.consider(self.line("Read a.java"))
        self.throttle._status_due._next_at = 0.0

        self.throttle.consider(self.line("Read a.java"))

        self.assertEqual(self.reported, ["Read a.java"])

    def test_is_inert_without_a_reporter(self):
        """No host callback means no reporting, and certainly no crash."""
        silent = acpx_process._ProgressRelay()

        silent.consider(self.line("Read a.java"))

    def test_ignores_a_line_that_is_not_json(self):
        self.throttle.consider("not json at all\n")

        self.assertEqual(self.reported, [])


class KeepAliveTests(unittest.TestCase):
    """The activity clock, which is a liveness proof rather than a status.

    The gateway abandons a turn after 1800 s with no activity. A delegation runs
    for up to 90 minutes, so whether this keeps ticking decides whether a
    working worker survives — one live 26-minute run was warned about at 23.
    """

    def setUp(self):
        self.alive = []
        self.relay = acpx_process._ProgressRelay(keep_alive=self.alive.append)

    def line(self, title, status="in_progress"):
        return json.dumps(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "title": title,
                        "status": status,
                    }
                },
            }
        )

    def test_reports_a_repeated_action_the_status_line_would_suppress(self):
        """The whole bug: one long step is work, not silence."""
        self.relay.consider(self.line("Run bun test"))
        self.relay._keep_alive_due._next_at = 0.0

        self.relay.consider(self.line("Run bun test"))

        self.assertEqual(self.alive, ["Run bun test", "Run bun test"])

    def test_counts_a_line_that_names_no_action(self):
        """Protocol chatter is still evidence the worker is alive."""
        self.relay.consider('{"method": "session/update", "params": {}}')

        self.assertEqual(self.alive, ["working"])

    def test_throttles_so_a_chatty_worker_costs_one_call(self):
        for index in range(50):
            self.relay.consider(self.line("Step {}".format(index)))

        self.assertEqual(len(self.alive), 1)

    def test_names_the_action_once_the_worker_has_one(self):
        self.relay.consider("noise\n")
        self.relay._keep_alive_due._next_at = 0.0

        self.relay.consider(self.line("Edit Fare.java"))

        self.assertEqual(self.alive, ["working", "Edit Fare.java"])


class StatusPhraseTests(unittest.TestCase):
    """The two sentences an operator reads during a delegation."""

    def test_names_the_worker_while_waiting_for_it(self):
        self.assertEqual(
            acpx_process.waiting_phrase("claude"), "Delegating task to claude worker…"
        )
        self.assertEqual(
            acpx_process.waiting_phrase("pi"), "Delegating task to pi worker…"
        )

    def test_reports_the_worker_and_its_action(self):
        self.assertEqual(
            acpx_process.activity_phrase("claude", "Read FareParser.java"),
            "claude worker: Read FareParser.java",
        )
        self.assertEqual(
            acpx_process.activity_phrase("pi", "Run bun test"), "pi worker: Run bun test"
        )

    def test_a_new_worker_needs_no_change_here(self):
        """The worker name is interpolated, never branched on."""
        self.assertEqual(
            acpx_process.activity_phrase("codex", "Edit Fare.java"),
            "codex worker: Edit Fare.java",
        )
        self.assertEqual(
            acpx_process.waiting_phrase("gemini"), "Delegating task to gemini worker…"
        )

    def test_stays_within_the_platform_status_limit(self):
        """Slack truncates its status line around 50 characters."""
        for worker in ("claude", "pi"):
            self.assertLessEqual(len(acpx_process.waiting_phrase(worker)), 50)


class ProgressRelayWiringTests(unittest.TestCase):
    """Which surfaces a run reports to, and what they are handed."""

    def setUp(self):
        self.finished = threading.Event()

    def test_is_none_when_the_host_offers_nothing(self):
        self.assertIsNone(
            acpx_process._progress_relay("claude", None, self.finished)
        )

    def test_is_none_when_the_host_registered_no_callbacks(self):
        """A host present but silent. Asserted directly rather than by relying
        on Hermes being absent from the test environment."""
        self.assertIsNone(
            acpx_process._progress_relay(
                "claude", acpx_process.HostProgress(), self.finished
            )
        )

    def test_both_surfaces_get_the_same_sentence(self):
        published = []
        alive = []
        relay = acpx_process._progress_relay(
            "claude",
            acpx_process.HostProgress(published.append, alive.append),
            self.finished,
        )

        relay.consider(_tool_call_line("Run bun test"))

        self.assertEqual(published, ["claude worker: Run bun test"])
        self.assertEqual(alive, ["claude worker: Run bun test"])

    def test_nothing_reports_once_the_run_is_over(self):
        """A reader outlives the run on the deadline path. A late phrase would
        paint a dead worker over whatever the host does next."""
        published = []
        relay = acpx_process._progress_relay(
            "claude", acpx_process.HostProgress(published.append), self.finished
        )
        self.finished.set()

        relay.consider(_tool_call_line("Run bun test"))

        self.assertEqual(published, [])

    def test_a_failing_host_surface_does_not_stop_the_run(self):
        def explode(_):
            raise RuntimeError("status surface is down")

        relay = acpx_process._progress_relay(
            "claude", acpx_process.HostProgress(explode, explode), self.finished
        )

        relay.consider(_tool_call_line("Run bun test"))


class ReaderSafetyTests(unittest.TestCase):
    def test_a_raising_relay_cannot_kill_the_reader(self):
        """This runs on the only thread draining stdout.

        An exception escaping here stops that drain, the worker blocks on a full
        pipe, and the run hangs — caused by the code meant to prove it has not.
        """
        class Explosive:
            def consider(self, line):
                raise RuntimeError("relay is unhappy")

        acpx_process._relay_line(Explosive(), "anything")

    def test_tolerates_a_line_the_parser_did_not_expect(self):
        """`params` is worker-supplied and need not be an object."""
        relay = acpx_process._ProgressRelay(report_status=lambda _: None)

        acpx_process._relay_line(
            relay, '{"method": "session/update", "params": "not an object"}'
        )


def _tool_call_line(title, status="in_progress"):
    return json.dumps(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": title,
                    "status": status,
                }
            },
        }
    )


class SessionModeTests(ProcessTestCase):
    """The worker runs in a named session so its mode can be set.

    The mode is the whole reason for the session. acpx sets none, so a worker
    left at the adapter's default asks permission for every action and acpx
    answers from the kind policy — which denies `execute` and left a review
    worker unable to run `git status`.
    """

    def recording_acpx(self, mode_exit=0):
        """An acpx stand-in that logs its control calls to a file."""
        log = os.path.join(self.workdir, "control.log")
        worker = os.path.join(self.workdir, "worker.py")
        with open(worker, "w", encoding="utf-8") as handle:
            handle.write(FAKE_WORKER)

        wrapper = os.path.join(self.workdir, "recording-acpx")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(
                '#!/bin/sh\n'
                'for arg in "$@"; do\n'
                '  case "$arg" in\n'
                '    sessions) echo "$@" >> "%s"; exit 0 ;;\n'
                '    set-mode) echo "$@" >> "%s"; exit %d ;;\n'
                '  esac\n'
                'done\n'
                'exec "%s" "%s" 0 0\n'
                % (log, log, mode_exit, sys.executable, worker)
            )
        os.chmod(wrapper, 0o755)
        return wrapper, log

    def control_calls(self, log):
        if not os.path.exists(log):
            return []
        with open(log, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def test_creates_a_named_session_and_sets_the_mode(self):
        binary, log = self.recording_acpx()

        self.run_task(binary)

        calls = self.control_calls(log)
        self.assertTrue(any("sessions new --name acp-" in c for c in calls), calls)
        self.assertTrue(any("set-mode auto" in c for c in calls), calls)

    def test_closes_the_session_afterwards(self):
        binary, log = self.recording_acpx()

        self.run_task(binary)

        calls = self.control_calls(log)
        self.assertTrue(any("sessions close acp-" in c for c in calls), calls)

    def test_a_failed_mode_change_fails_the_run(self):
        """Continuing in the wrong mode reproduces the original fault: a worker
        that cannot act, reporting something else as the reason."""
        binary, _ = self.recording_acpx(mode_exit=1)

        with self.assertRaises(acpx_process.SpawnError) as caught:
            self.run_task(binary)

        self.assertIn("mode", str(caught.exception))

    def test_the_session_name_carries_the_run_id(self):
        """The lease, the settings overlay and the session share one id, so the
        artefacts a delegation leaves behind can be traced to each other."""
        request = acpx_process.RunRequest(
            acpx_bin="acpx",
            worker="claude",
            task="t",
            working_directory=self.workdir,
            timeout_seconds=30,
            kind_policy=KIND_POLICY,
            grace_seconds=5,
            lease_id="deadbeef",
        )

        self.assertEqual(acpx_process._session_name(request), "acp-deadbeef")
        self.assertEqual(request.permission_mode, "auto")


class CommandTests(ProcessTestCase):
    def test_passes_the_policy_and_cwd_to_acpx(self):
        command = acpx_process._build_command(
            acpx_process.RunRequest(
                acpx_bin="acpx",
                worker="pi",
                task="anything",
                working_directory="/tmp/repo",
                timeout_seconds=300,
                kind_policy=KIND_POLICY,
                grace_seconds=5,
                lease_id="testlease",
            )
        )

        self.assertEqual(command[0], "acpx")
        self.assertIn("--permission-policy", command)
        self.assertEqual(json.loads(command[command.index("--permission-policy") + 1]), KIND_POLICY)
        self.assertEqual(command[command.index("--cwd") + 1], "/tmp/repo")

    def test_opts_into_the_user_setting_source(self):
        """Without this, ~/.claude/commands is invisible to the worker.

        acpx defaults Claude Code to ["project", "local"], so a delegation
        naming an operator-installed command comes back "Unknown command: …" at
        exit 0 — improvised, not failed.
        """
        environment = acpx_process._build_environment()

        self.assertEqual(environment["ACPX_CLAUDE_INCLUDE_USER_SETTINGS"], "1")

    def test_keeps_the_rest_of_the_environment(self):
        os.environ["ACP_TEST_MARKER"] = "kept"
        self.addCleanup(os.environ.pop, "ACP_TEST_MARKER", None)

        self.assertEqual(acpx_process._build_environment()["ACP_TEST_MARKER"], "kept")

    def test_sends_the_task_over_stdin_rather_than_argv(self):
        """Argv quoting and length limits are not worth risking on a prompt."""
        command = acpx_process._build_command(
            acpx_process.RunRequest(
                acpx_bin="acpx",
                worker="claude",
                task="a very long prompt",
                working_directory="/tmp/repo",
                timeout_seconds=300,
                kind_policy=KIND_POLICY,
                grace_seconds=5,
                lease_id="testlease",
            )
        )

        self.assertEqual(command[-3:], ["prompt", "-f", "-"])
        # A NAMED session, not a one-shot exec: the mode can only be set on a
        # session, and the mode is what lets the worker run anything.
        self.assertIn("-s", command)
        self.assertNotIn("a very long prompt", command)


if __name__ == "__main__":
    unittest.main()
