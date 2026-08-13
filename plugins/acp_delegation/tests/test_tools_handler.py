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
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

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
        args = {"worker": "claude", "task": "do the thing", "cwd": self.root}
        args.update(overrides)
        return json.loads(tools.handle_acp_delegate(args))


class ValidationTests(HandlerTestCase):
    def test_rejects_an_unsupported_worker(self):
        self.assertEqual(self.delegate(worker="gpt")["error_type"], "invalid_worker")

    def test_rejects_an_empty_task(self):
        self.assertEqual(self.delegate(task="   ")["error_type"], "invalid_task")

    def test_rejects_a_directory_outside_the_allowed_roots(self):
        self.assertEqual(self.delegate(cwd="/etc")["error_type"], "invalid_cwd")

    def test_reports_an_unconfigured_plugin_distinctly_from_a_bad_argument(self):
        tools._load_hermes_config = lambda: {}

        self.assertEqual(self.delegate()["error_type"], "not_configured")

    def test_never_spawns_acpx_when_validation_fails(self):
        self.stub_run(outcome())

        self.delegate(cwd="/etc")

        self.assertEqual(self.calls, [])


class SuccessTests(HandlerTestCase):
    def test_returns_the_workers_reply_and_context(self):
        self.stub_run(outcome(text="all done"))

        result = self.delegate()

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "all done")
        self.assertEqual(result["worker"], "claude")
        self.assertEqual(result["cwd"], self.root)
        self.assertIn("duration_seconds", result)

    def test_passes_the_validated_request_through_to_acpx(self):
        self.stub_run(outcome())

        self.delegate(worker="pi", task="fix it")

        call = self.calls[0]
        self.assertEqual(call["worker"], "pi")
        self.assertEqual(call["task"], "fix it")
        self.assertEqual(call["working_directory"], self.root)

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

        raw = tools.handle_acp_delegate({"worker": "claude", "task": "x", "cwd": self.root})

        self.assertIsInstance(raw, str)
        self.assertIsInstance(json.loads(raw), dict)


class OverlayLifecycleTests(HandlerTestCase):
    def settings_file(self):
        return os.path.join(self.root, ".claude", "settings.local.json")

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
