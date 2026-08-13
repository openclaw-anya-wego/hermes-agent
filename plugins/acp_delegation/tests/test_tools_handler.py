"""Tests for the tool handler.

These exercise the boundary Hermes actually sees: arguments in, a JSON string
out, on every path. acpx is stubbed, so nothing here spawns a worker or spends
tokens.
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

from plugins.acp_delegation import acpx_process, parse, tools  # noqa: E402


def outcome(text="done", exit_code=0, output_tokens=5):
    transcript = parse.Transcript()
    if text:
        transcript.text_fragments.append(text)
    transcript.saw_final_result = True
    transcript.stop_reason = "end_turn"
    transcript.usage = parse.Usage(output_tokens=output_tokens, total_tokens=31206)
    return acpx_process.RunOutcome(transcript, exit_code, "")


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        # The allowed root holds projects; it is not one itself. The worker runs
        # in a project under it, resolved per request.
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.project = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.project, ".git"))

        self.original_config = tools._load_hermes_config
        self.original_run = acpx_process.run
        self.addCleanup(self._restore)

        tools._load_hermes_config = lambda: {
            "plugins": {"entries": {"acp_delegation": {"allowed_cwd_roots": [self.root]}}}
        }
        self.calls = []

    def _restore(self):
        tools._load_hermes_config = self.original_config
        acpx_process.run = self.original_run

    def stub_run(self, result):
        def _run(**kwargs):
            self.calls.append(kwargs)
            if isinstance(result, Exception):
                raise result
            return result

        acpx_process.run = _run

    def delegate(self, **overrides):
        args = {"worker": "claude", "task": "do the thing", "cwd": self.project}
        args.update(overrides)
        return json.loads(tools.handle_acp_delegate(args))


class ValidationTests(HandlerTestCase):
    def test_rejects_an_unsupported_worker(self):
        self.assertEqual(self.delegate(worker="gpt")["error_type"], "invalid_worker")

    def test_rejects_an_empty_task(self):
        self.assertEqual(self.delegate(task="   ")["error_type"], "invalid_task")

    def test_rejects_a_directory_that_merely_contains_projects(self):
        """Pins the wiring: the marker setting has to reach the resolver."""
        self.assertEqual(self.delegate(cwd=self.root)["error_type"], "invalid_cwd")

    def test_runs_a_subdirectory_request_in_its_project(self):
        deep = os.path.join(self.project, "src")
        os.makedirs(deep)
        self.stub_run(outcome("done"))

        result = self.delegate(cwd=deep)

        self.assertEqual(result["cwd"], self.project)
        self.assertEqual(self.calls[0]["working_directory"], self.project)

    def test_rejects_a_directory_outside_the_allowed_roots(self):
        self.assertEqual(self.delegate(cwd="/etc")["error_type"], "invalid_cwd")

    def test_reports_an_unconfigured_plugin_distinctly_from_a_bad_argument(self):
        tools._load_hermes_config = lambda: {}

        self.assertEqual(self.delegate()["error_type"], "not_configured")

    def test_never_spawns_acpx_when_validation_fails(self):
        self.stub_run(outcome())

        self.delegate(cwd="/etc")

        self.assertEqual(self.calls, [])


class CommandTests(HandlerTestCase):
    """The slash command is a separate slot, not a convention inside `task`.

    Measured on `draft-github-issue` over five runs: every template slot was
    filled, and every requirement written as prose fired zero times. A schema
    property is a slot; a sentence in a description is prose.
    """

    def prompt(self):
        return self.calls[0]["task"]

    def test_puts_the_command_before_the_brief(self):
        self.stub_run(outcome())

        self.delegate(command="/saber-code-review #1234", task="focus on the parser")

        self.assertEqual(self.prompt(), "/saber-code-review #1234\nfocus on the parser")

    def test_sends_the_brief_alone_when_no_command_is_given(self):
        self.stub_run(outcome())

        self.delegate(task="just fix it")

        self.assertEqual(self.prompt(), "just fix it")

    def test_echoes_the_command_in_the_result(self):
        """Commands resolve per project, so the call site does not say which ran."""
        self.stub_run(outcome())

        result = self.delegate(command="/deploy-mini", task="ship it")

        self.assertEqual(result["command"], "/deploy-mini")

    def test_omits_the_command_from_a_result_that_had_none(self):
        self.stub_run(outcome())

        self.assertNotIn("command", self.delegate(task="just fix it"))

    def test_accepts_a_command_for_either_worker(self):
        self.stub_run(outcome())

        self.delegate(worker="pi", command="/review", task="look at it")

        self.assertTrue(self.prompt().startswith("/review\n"))

    def test_rejects_a_command_that_is_not_a_command(self):
        self.assertEqual(
            self.delegate(command="saber-code-review")["error_type"], "invalid_command"
        )

    def test_rejects_a_multi_line_command(self):
        """Otherwise it becomes a second instruction competing with `task`."""
        result = self.delegate(command="/review\nand also rewrite the tests")

        self.assertEqual(result["error_type"], "invalid_command")

    def test_rejects_a_bare_slash(self):
        self.assertEqual(self.delegate(command="/")["error_type"], "invalid_command")

    def test_rejects_a_command_that_is_not_a_string(self):
        self.assertEqual(self.delegate(command={"name": "/x"})["error_type"], "invalid_command")

    def test_treats_a_blank_command_as_absent(self):
        self.stub_run(outcome())

        self.delegate(command="   ", task="just fix it")

        self.assertEqual(self.prompt(), "just fix it")

    def test_never_spawns_acpx_for_a_malformed_command(self):
        self.stub_run(outcome())

        self.delegate(command="not a command")

        self.assertEqual(self.calls, [])


class SuccessTests(HandlerTestCase):
    def test_returns_the_workers_reply_and_context(self):
        self.stub_run(outcome(text="all done"))

        result = self.delegate()

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "all done")
        self.assertEqual(result["worker"], "claude")
        self.assertEqual(result["cwd"], self.project)
        self.assertIn("duration_seconds", result)

    def test_passes_the_validated_request_through_to_acpx(self):
        self.stub_run(outcome())

        self.delegate(worker="pi", task="fix it")

        call = self.calls[0]
        self.assertEqual(call["worker"], "pi")
        self.assertEqual(call["task"], "fix it")
        self.assertEqual(call["working_directory"], self.project)

    def test_clamps_an_absurd_timeout_before_spawning(self):
        self.stub_run(outcome())

        self.delegate(timeout_seconds=999999)

        self.assertEqual(self.calls[0]["timeout_seconds"], 3600)

    def test_applies_the_default_timeout_when_none_is_given(self):
        self.stub_run(outcome())

        self.delegate()

        self.assertEqual(self.calls[0]["timeout_seconds"], 900)


class FailureTests(HandlerTestCase):
    def test_reports_a_missing_acpx_binary_actionably(self):
        self.stub_run(acpx_process.SpawnError("acpx was not found", "acpx_not_found"))

        result = self.delegate()

        self.assertEqual(result["error_type"], "acpx_not_found")
        self.assertIn("acpx", result["error"])

    def test_surfaces_a_false_success_as_a_failure(self):
        self.stub_run(outcome(text="", output_tokens=0))

        result = self.delegate()

        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "false_success")

    def test_reports_a_nonzero_exit_with_its_mapped_type(self):
        self.stub_run(outcome(exit_code=5))

        self.assertEqual(self.delegate()["error_type"], "permission_denied")

    def test_always_returns_a_json_string(self):
        self.stub_run(outcome(exit_code=1))

        raw = tools.handle_acp_delegate({"worker": "claude", "task": "x", "cwd": self.project})

        self.assertIsInstance(raw, str)
        self.assertIsInstance(json.loads(raw), dict)


class OverlayLifecycleTests(HandlerTestCase):
    def settings_file(self):
        return os.path.join(self.project, ".claude", "settings.local.json")

    def test_installs_deny_rules_while_the_worker_runs(self):
        seen = {}

        def _run(**kwargs):
            del kwargs
            with open(self.settings_file(), "r", encoding="utf-8") as handle:
                seen["deny"] = json.load(handle)["permissions"]["deny"]
            return outcome()

        acpx_process.run = _run
        self.delegate()

        self.assertIn("Edit(~/.openclaw/**)", seen["deny"])

    def test_removes_the_overlay_after_a_successful_run(self):
        self.stub_run(outcome())

        self.delegate()

        self.assertFalse(os.path.exists(self.settings_file()))

    def test_removes_the_overlay_even_when_the_spawn_fails(self):
        self.stub_run(acpx_process.SpawnError("nope", "acpx_not_found"))

        self.delegate()

        self.assertFalse(os.path.exists(self.settings_file()))


if __name__ == "__main__":
    unittest.main()
