"""Tests for the path-level permission overlay.

The overlay writes into a real repository's working tree, so putting it back
matters more than anything it does while installed. Most of these are about the
two ways the old snapshot-based restore lost: a run that never got to restore,
and two runs sharing one checkout.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings_overlay  # noqa: E402

DENY_RULES = ["Edit(~/.openclaw/**)", "Edit(~/clawd/**)"]

# A pid that cannot be running. Nothing is ever assigned pid 0, and kill(0, 0)
# addresses the process group rather than a process, so the module special-cases
# nothing to make this work.
DEAD_PID = -12345


class OverlayTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.settings_path = os.path.join(self.workdir, ".claude", "settings.local.json")

    def install(self, rules=DENY_RULES, run_id="run-1"):
        return settings_overlay.install(self.workdir, rules, run_id)

    def write_existing(self, payload):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write(payload)

    def read_settings(self):
        with open(self.settings_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def deny_rules(self):
        return self.read_settings().get("permissions", {}).get("deny", [])


class InstallTests(OverlayTestCase):
    def test_creates_the_settings_file_with_the_deny_rules(self):
        self.install()

        self.assertEqual(self.deny_rules(), DENY_RULES)

    def test_installs_nothing_when_there_are_no_rules(self):
        handle = self.install(rules=[])

        self.assertIsNone(handle)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_keeps_a_repositorys_own_deny_rules(self):
        self.write_existing(json.dumps({"permissions": {"deny": ["Read(./.env)"]}}))

        self.install()

        self.assertEqual(self.deny_rules(), ["Read(./.env)"] + DENY_RULES)

    def test_keeps_unrelated_settings_untouched(self):
        self.write_existing(json.dumps({"model": "opus", "permissions": {"allow": ["Read(./**)"]}}))

        self.install()

        settings = self.read_settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["permissions"]["allow"], ["Read(./**)"])

    def test_does_not_duplicate_a_rule_that_is_already_present(self):
        self.write_existing(json.dumps({"permissions": {"deny": [DENY_RULES[0]]}}))

        self.install()

        self.assertEqual(self.deny_rules(), DENY_RULES)

    def test_treats_a_malformed_settings_file_as_absent_rather_than_failing(self):
        self.write_existing("{ this is not json")

        self.install()

        self.assertEqual(self.deny_rules(), DENY_RULES)

    def test_records_the_run_and_what_it_requires(self):
        """The marker is what lets restore tell our rules from the operator's."""
        self.install(run_id="abc")

        marker = self.read_settings()[settings_overlay.OVERLAY_KEY]
        self.assertEqual(len(marker["holders"]), 1)
        self.assertEqual(marker["holders"][0]["run"], "abc")
        self.assertEqual(marker["holders"][0]["rules"], DENY_RULES)
        self.assertEqual(marker["holders"][0]["pid"], os.getpid())

    def test_claims_ownership_only_of_the_rules_it_introduced(self):
        """A rule the repository already had is not ours to withdraw later."""
        self.write_existing(json.dumps({"permissions": {"deny": [DENY_RULES[0]]}}))

        self.install()

        marker = self.read_settings()[settings_overlay.OVERLAY_KEY]
        self.assertEqual(marker["owned"], [DENY_RULES[1]])
        self.assertEqual(marker["holders"][0]["rules"], DENY_RULES)


class RestoreTests(OverlayTestCase):
    def test_deletes_the_file_it_created(self):
        handle = self.install()

        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(self.settings_path))

    def test_removes_the_claude_directory_it_created(self):
        handle = self.install()

        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(os.path.join(self.workdir, ".claude")))

    def test_leaves_a_preexisting_claude_directory_in_place(self):
        os.makedirs(os.path.join(self.workdir, ".claude"))

        handle = self.install()
        settings_overlay.restore(handle)

        self.assertTrue(os.path.isdir(os.path.join(self.workdir, ".claude")))

    def test_restores_the_original_settings(self):
        """Content, not bytes.

        Byte-for-byte restore needs a snapshot, and a snapshot is what cannot
        survive a crash or a second writer. The file is regenerated from what is
        on disk at restore time, so indentation is this module's, and every
        setting the operator wrote is still there and still means the same thing.
        """
        self.write_existing(json.dumps({"model": "opus", "permissions": {"deny": ["Read(./.env)"]}}))

        handle = self.install()
        settings_overlay.restore(handle)

        self.assertEqual(
            self.read_settings(), {"model": "opus", "permissions": {"deny": ["Read(./.env)"]}}
        )

    def test_leaves_no_marker_behind(self):
        self.write_existing(json.dumps({"model": "opus"}))

        handle = self.install()
        settings_overlay.restore(handle)

        self.assertNotIn(settings_overlay.OVERLAY_KEY, self.read_settings())

    def test_does_not_withdraw_a_rule_the_repository_already_had(self):
        self.write_existing(json.dumps({"permissions": {"deny": [DENY_RULES[0]]}}))

        handle = self.install()
        settings_overlay.restore(handle)

        self.assertEqual(self.deny_rules(), [DENY_RULES[0]])

    def test_keeps_an_edit_made_while_the_worker_ran(self):
        """The operator works in this checkout too."""
        handle = self.install()
        settings = self.read_settings()
        settings["model"] = "haiku"
        self.write_existing(json.dumps(settings))

        settings_overlay.restore(handle)

        self.assertEqual(self.read_settings(), {"model": "haiku"})

    def test_restoring_nothing_is_harmless(self):
        settings_overlay.restore(None)

    def test_restoring_twice_is_harmless(self):
        handle = self.install()

        settings_overlay.restore(handle)
        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(self.settings_path))


class ConcurrentRunTests(OverlayTestCase):
    """Two delegations, one checkout. Snapshot restore could not survive this."""

    def test_the_first_to_finish_does_not_disarm_the_second(self):
        first = self.install(run_id="first")
        second = self.install(run_id="second")

        settings_overlay.restore(first)

        self.assertEqual(self.deny_rules(), DENY_RULES)

    def test_the_last_to_finish_cleans_up(self):
        first = self.install(run_id="first")
        second = self.install(run_id="second")

        settings_overlay.restore(first)
        settings_overlay.restore(second)

        self.assertFalse(os.path.exists(self.settings_path))

    def test_restoring_out_of_order_still_cleans_up(self):
        first = self.install(run_id="first")
        second = self.install(run_id="second")

        settings_overlay.restore(second)
        settings_overlay.restore(first)

        self.assertFalse(os.path.exists(self.settings_path))


class SelfHealTests(OverlayTestCase):
    """A run killed before it could restore leaves rules in the operator's repo."""

    def leave_residue(self, pid, run_id="ghost"):
        self.write_existing(
            json.dumps(
                {
                    "permissions": {"deny": list(DENY_RULES)},
                    settings_overlay.OVERLAY_KEY: {
                        "holders": [{"run": run_id, "pid": pid, "rules": list(DENY_RULES)}],
                        "owned": list(DENY_RULES),
                    },
                }
            )
        )

    def test_withdraws_rules_left_by_a_process_that_is_gone(self):
        self.leave_residue(DEAD_PID)

        handle = self.install(run_id="live")
        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(self.settings_path))

    def test_keeps_rules_belonging_to_a_run_still_going(self):
        self.leave_residue(os.getpid(), run_id="still-running")

        handle = self.install(run_id="live")
        settings_overlay.restore(handle)

        self.assertEqual(self.deny_rules(), DENY_RULES)

    def test_a_stale_marker_does_not_destroy_the_operators_own_settings(self):
        self.write_existing(
            json.dumps(
                {
                    "model": "opus",
                    "permissions": {"deny": ["Read(./.env)"] + DENY_RULES},
                    settings_overlay.OVERLAY_KEY: {
                        "holders": [{"run": "ghost", "pid": DEAD_PID, "rules": list(DENY_RULES)}],
                        "owned": list(DENY_RULES),
                    },
                }
            )
        )

        handle = self.install(run_id="live")
        settings_overlay.restore(handle)

        self.assertEqual(
            self.read_settings(), {"model": "opus", "permissions": {"deny": ["Read(./.env)"]}}
        )


if __name__ == "__main__":
    unittest.main()
