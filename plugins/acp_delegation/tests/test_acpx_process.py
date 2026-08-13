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
import unittest

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

        wrapper = os.path.join(self.workdir, "fake-acpx")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(
                '#!/bin/sh\nexec "{}" "{}" {} {}\n'.format(
                    sys.executable, script, chatter, exit_code
                )
            )
        os.chmod(wrapper, 0o755)
        return wrapper

    def run_task(self, binary, task="hello", timeout_seconds=30, grace_seconds=5):
        return acpx_process.run(
            acpx_bin=binary,
            worker="claude",
            task=task,
            working_directory=self.workdir,
            timeout_seconds=timeout_seconds,
            kind_policy=KIND_POLICY,
            grace_seconds=grace_seconds,
            lease_id="testlease",
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


class CommandTests(ProcessTestCase):
    def test_passes_the_policy_and_cwd_to_acpx(self):
        command = acpx_process._build_command("acpx", "pi", "/tmp/repo", 300, KIND_POLICY)

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
        command = acpx_process._build_command("acpx", "claude", "/tmp/repo", 300, KIND_POLICY)

        self.assertEqual(command[-3:], ["exec", "-f", "-"])


if __name__ == "__main__":
    unittest.main()
