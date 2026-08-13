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
    """Admission and project anchoring.

    `self.root` stands in for a `working-repos` holding many checkouts, and
    `self.inside` for one of them. The root deliberately has no project marker:
    that is the case the anchoring exists to refuse.
    """

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.inside = self.make_project("repo")

    def make_project(self, *segments, marker=".git"):
        path = os.path.join(self.root, *segments)
        os.makedirs(os.path.join(path, marker), exist_ok=True)
        return path

    def test_accepts_a_project_inside_an_allowed_root(self):
        resolved = config.resolve_working_directory(self.inside, [self.root])

        self.assertEqual(resolved, self.inside)

    def test_accepts_the_root_itself_when_the_root_is_a_project(self):
        os.makedirs(os.path.join(self.root, ".git"))

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


class ProjectAnchoringTests(ResolveWorkingDirectoryTests):
    """Which project a request lands in is derived per call, not configured.

    One allowed root holds many checkouts, and the worker's cwd is what decides
    whose settings, agents and hooks it loads. Anchoring wrong is silent: the run
    simply proceeds with none of the project's configuration.
    """

    def test_resolves_a_subdirectory_up_to_its_project(self):
        deep = os.path.join(self.inside, "src", "app")
        os.makedirs(deep)

        self.assertEqual(config.resolve_working_directory(deep, [self.root]), self.inside)

    def test_refuses_a_directory_that_merely_contains_projects(self):
        """The bug this exists for: `working-repos` itself is not a project."""
        with self.assertRaises(config.ConfigurationError) as raised:
            config.resolve_working_directory(self.root, [self.root])

        self.assertEqual(raised.exception.error_type, "invalid_cwd")
        self.assertIn("not inside a project", str(raised.exception))

    def test_sends_two_requests_to_two_different_projects(self):
        other = self.make_project("other-repo")

        self.assertEqual(config.resolve_working_directory(self.inside, [self.root]), self.inside)
        self.assertEqual(config.resolve_working_directory(other, [self.root]), other)

    def test_never_anchors_above_the_allowed_root(self):
        """A `working-repos` sitting inside some larger checkout must not escape."""
        os.makedirs(os.path.join(self.root, ".git"))
        container = os.path.join(self.root, "projects")
        plain = os.path.join(container, "notes")
        os.makedirs(plain)

        with self.assertRaises(config.ConfigurationError):
            config.resolve_working_directory(plain, [container])

    def test_recognises_other_version_control_systems(self):
        mercurial = self.make_project("hg-repo", marker=".hg")

        self.assertEqual(config.resolve_working_directory(mercurial, [self.root]), mercurial)

    def test_honours_operator_supplied_markers(self):
        """A project directory that is not a checkout is the operator's call."""
        plain = self.make_project("no-vcs", marker="pyproject.toml")

        self.assertEqual(
            config.resolve_working_directory(plain, [self.root], ["pyproject.toml"]), plain
        )

    def test_does_not_anchor_on_an_agent_tools_config_directory(self):
        """`.claude` would resolve the same path differently per worker.

        A package holding Claude settings but no pi settings would be the
        project root for one worker and not the other, so the marker set stays
        tool-neutral.
        """
        package = os.path.join(self.inside, "packages", "web")
        os.makedirs(os.path.join(package, ".claude"))

        self.assertEqual(config.resolve_working_directory(package, [self.root]), self.inside)


class ProjectMarkerSettingTests(unittest.TestCase):
    def test_defaults_to_the_version_control_roots(self):
        settings = config.load_settings(config_with(allowed_cwd_roots=["/tmp"]))

        self.assertEqual(settings.project_markers, list(config.DEFAULT_PROJECT_MARKERS))

    def test_reads_an_operator_supplied_list(self):
        settings = config.load_settings(
            config_with(allowed_cwd_roots=["/tmp"], project_markers=["pyproject.toml"])
        )

        self.assertEqual(settings.project_markers, ["pyproject.toml"])

    def test_ignores_a_marker_containing_a_path_separator(self):
        """These are compared against directory entries, so `a/b` never matches."""
        settings = config.load_settings(
            config_with(allowed_cwd_roots=["/tmp"], project_markers=["a/b"])
        )

        self.assertEqual(settings.project_markers, list(config.DEFAULT_PROJECT_MARKERS))

    def test_falls_back_when_the_setting_is_not_a_list(self):
        settings = config.load_settings(
            config_with(allowed_cwd_roots=["/tmp"], project_markers=".git")
        )

        self.assertEqual(settings.project_markers, list(config.DEFAULT_PROJECT_MARKERS))


if __name__ == "__main__":
    unittest.main()
