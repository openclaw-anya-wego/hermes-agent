"""Tests for the path-level permission overlay.

The overlay writes into a real repository's working tree, so putting it back
exactly as it was matters more than anything it does while installed.
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


class OverlayTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.settings_path = os.path.join(self.workdir, ".claude", "settings.local.json")

    def write_existing(self, payload):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write(payload)

    def read_settings(self):
        with open(self.settings_path, "r", encoding="utf-8") as handle:
            return json.load(handle)


class InstallTests(OverlayTestCase):
    def test_creates_the_settings_file_with_the_deny_rules(self):
        settings_overlay.install(self.workdir, DENY_RULES)

        self.assertEqual(self.read_settings()["permissions"]["deny"], DENY_RULES)

    def test_installs_nothing_when_there_are_no_rules(self):
        handle = settings_overlay.install(self.workdir, [])

        self.assertIsNone(handle)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_keeps_a_repositorys_own_deny_rules(self):
        self.write_existing(json.dumps({"permissions": {"deny": ["Read(./.env)"]}}))

        settings_overlay.install(self.workdir, DENY_RULES)

        self.assertEqual(
            self.read_settings()["permissions"]["deny"], ["Read(./.env)"] + DENY_RULES
        )

    def test_keeps_unrelated_settings_untouched(self):
        self.write_existing(json.dumps({"model": "opus", "permissions": {"allow": ["Read(./**)"]}}))

        settings_overlay.install(self.workdir, DENY_RULES)

        settings = self.read_settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["permissions"]["allow"], ["Read(./**)"])

    def test_does_not_duplicate_a_rule_that_is_already_present(self):
        self.write_existing(json.dumps({"permissions": {"deny": [DENY_RULES[0]]}}))

        settings_overlay.install(self.workdir, DENY_RULES)

        self.assertEqual(self.read_settings()["permissions"]["deny"], DENY_RULES)

    def test_treats_a_malformed_settings_file_as_absent_rather_than_failing(self):
        self.write_existing("{ this is not json")

        settings_overlay.install(self.workdir, DENY_RULES)

        self.assertEqual(self.read_settings()["permissions"]["deny"], DENY_RULES)


class RestoreTests(OverlayTestCase):
    def test_deletes_the_file_it_created(self):
        handle = settings_overlay.install(self.workdir, DENY_RULES)

        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(self.settings_path))

    def test_removes_the_claude_directory_it_created(self):
        handle = settings_overlay.install(self.workdir, DENY_RULES)

        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(os.path.join(self.workdir, ".claude")))

    def test_leaves_a_preexisting_claude_directory_in_place(self):
        os.makedirs(os.path.join(self.workdir, ".claude"))

        handle = settings_overlay.install(self.workdir, DENY_RULES)
        settings_overlay.restore(handle)

        self.assertTrue(os.path.isdir(os.path.join(self.workdir, ".claude")))

    def test_restores_the_original_file_byte_for_byte(self):
        original = json.dumps({"permissions": {"deny": ["Read(./.env)"]}}, indent=4) + "\n"
        self.write_existing(original)

        handle = settings_overlay.install(self.workdir, DENY_RULES)
        settings_overlay.restore(handle)

        with open(self.settings_path, "r", encoding="utf-8") as content:
            self.assertEqual(content.read(), original)

    def test_restoring_nothing_is_harmless(self):
        settings_overlay.restore(None)

    def test_restoring_twice_is_harmless(self):
        handle = settings_overlay.install(self.workdir, DENY_RULES)

        settings_overlay.restore(handle)
        settings_overlay.restore(handle)

        self.assertFalse(os.path.exists(self.settings_path))


if __name__ == "__main__":
    unittest.main()
