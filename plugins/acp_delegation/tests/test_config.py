"""Tests for settings parsing and working-directory admission.

These rules are the plugin's security boundary: everything a delegated worker is
allowed to touch is decided here, so the failure directions matter as much as
the success ones.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def config_with(**entry):
    return {"plugins": {"entries": {config.PLUGIN_ID: entry}}}


class LoadSettingsTests(unittest.TestCase):
    def test_refuses_to_run_when_no_roots_are_configured(self):
        with self.assertRaises(config.ConfigurationError) as raised:
            config.load_settings(config_with())

        self.assertIn("allowed_cwd_roots", str(raised.exception))

    def test_refuses_an_empty_config_rather_than_allowing_everything(self):
        with self.assertRaises(config.ConfigurationError):
            config.load_settings({})

    def test_refuses_a_missing_config_rather_than_allowing_everything(self):
        with self.assertRaises(config.ConfigurationError):
            config.load_settings(None)

    def test_reads_the_configured_roots_and_binary(self):
        settings = config.load_settings(config_with(allowed_cwd_roots=["/tmp"], acpx_bin="/x/acpx"))

        self.assertEqual(settings.acpx_bin, "/x/acpx")
        self.assertEqual(settings.allowed_cwd_roots, [os.path.realpath("/tmp")])

    def test_falls_back_to_defaults_for_unset_optional_values(self):
        settings = config.load_settings(config_with(allowed_cwd_roots=["/tmp"]))

        self.assertEqual(settings.acpx_bin, "acpx")
        self.assertEqual(settings.default_timeout_seconds, config.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(settings.kind_policy["defaultAction"], "deny")

    def test_ignores_a_nonsense_timeout_instead_of_crashing(self):
        settings = config.load_settings(
            config_with(allowed_cwd_roots=["/tmp"], default_timeout_seconds="soon")
        )

        self.assertEqual(settings.default_timeout_seconds, config.DEFAULT_TIMEOUT_SECONDS)

    def test_ignores_roots_that_are_not_a_list(self):
        with self.assertRaises(config.ConfigurationError):
            config.load_settings(config_with(allowed_cwd_roots="/tmp"))


class ClampTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.settings = config.load_settings(
            config_with(allowed_cwd_roots=["/tmp"], max_timeout_seconds=1000)
        )

    def test_uses_the_default_when_none_is_requested(self):
        self.assertEqual(self.settings.clamp_timeout(None), config.DEFAULT_TIMEOUT_SECONDS)

    def test_caps_a_request_above_the_maximum(self):
        self.assertEqual(self.settings.clamp_timeout(99999), 1000)

    def test_raises_a_request_below_the_minimum(self):
        self.assertEqual(self.settings.clamp_timeout(1), config.MIN_TIMEOUT_SECONDS)

    def test_keeps_a_request_inside_the_range(self):
        self.assertEqual(self.settings.clamp_timeout(600), 600)


class ResolveWorkingDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.inside = os.path.join(self.root, "repo")
        os.makedirs(self.inside)

    def test_accepts_a_directory_inside_an_allowed_root(self):
        resolved = config.resolve_working_directory(self.inside, [self.root])

        self.assertEqual(resolved, self.inside)

    def test_accepts_the_root_itself(self):
        self.assertEqual(config.resolve_working_directory(self.root, [self.root]), self.root)

    def test_rejects_a_directory_outside_every_root(self):
        outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)

        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(outside, [self.root])

    def test_rejects_a_sibling_whose_name_merely_starts_with_the_root(self):
        """`/tmp/rootEVIL` must not pass a check against `/tmp/root`."""
        sibling = self.root + "EVIL"
        os.makedirs(sibling)
        self.addCleanup(shutil.rmtree, sibling, ignore_errors=True)

        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(sibling, [self.root])

    def test_rejects_a_symlink_that_escapes_an_allowed_root(self):
        """The link sits inside the root; its target does not."""
        outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link = os.path.join(self.root, "escape")
        os.symlink(outside, link)

        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(link, [self.root])

    def test_rejects_a_path_that_does_not_exist(self):
        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(os.path.join(self.root, "nope"), [self.root])

    def test_rejects_a_file_that_is_not_a_directory(self):
        target = os.path.join(self.inside, "file.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")

        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(target, [self.root])

    def test_rejects_an_empty_path(self):
        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory("   ", [self.root])

    def test_rejects_everything_when_no_roots_are_given(self):
        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(self.inside, [])


if __name__ == "__main__":
    unittest.main()
