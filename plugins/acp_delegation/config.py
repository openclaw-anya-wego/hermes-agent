"""Settings for the ACP delegation plugin, read from ``plugins.entries``.

Pure by design: this module parses a plain dictionary and never loads anything
itself, so ``tools.py`` owns the single call into Hermes config and every rule
here stays testable offline.

Config lives under ``plugins.entries.acp_delegation`` in ``config.yaml``::

    plugins:
      entries:
        acp_delegation:
          allowed_cwd_roots:
            - /Users/wegoaiteam/working-repos
          acpx_bin: /Users/wegoaiteam/.local/node/bin/acpx
          default_timeout_seconds: 900
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PLUGIN_ID = "acp_delegation"

DEFAULT_TIMEOUT_SECONDS = 900
MAX_TIMEOUT_SECONDS = 3600
MIN_TIMEOUT_SECONDS = 30
# Well above what a reply normally reaches. Hermes spills anything larger than
# `max_result_size_chars` to a file and gives the model a path to read it, so a
# low plugin-side cap would throw away what the host would have preserved.
DEFAULT_MAX_RESPONSE_CHARS = 100000

# Time allowed after acpx's own --timeout should have fired. acpx is expected to
# stop the worker itself; this margin only covers acpx hanging, which is why it
# is short.
DEADLINE_GRACE_SECONDS = 30

# What marks a directory as a project. The delegated worker's cwd decides which
# project config it loads — settings, agents, hooks, MCP servers — so pointing it
# at a directory that merely *holds* projects silently gives it none of theirs.
#
# Deliberately a version-control root and not a tool's config file. Anchoring on
# `.claude/` would resolve the same path differently per worker: a monorepo
# package holding Claude settings but no pi settings would become the project
# root for one worker and not the other, and every new tool would mean another
# entry here. A repository root means the same thing to all of them.
#
# Operators with project directories that are not checkouts can override this
# with `project_markers`.
DEFAULT_PROJECT_MARKERS = (".git", ".hg", ".svn")

# Kind-level permissions. acpx matches tool kinds and names, never paths, so
# path scoping is the settings-overlay layer's job — see settings_overlay.py.
DEFAULT_KIND_POLICY: Dict[str, Any] = {
    "autoApprove": ["read", "search", "edit"],
    "autoDeny": [],
    "escalate": [],
    "defaultAction": "deny",
}

# The ACP session mode the worker runs in.
#
# "auto" lets the worker's own model classifier approve or deny each action.
# That is what makes a review possible at all: reviewing code means running
# `git`, `gh` and tests, which are `execute`, and the kind policy above denies
# `execute` — acpx matches kinds and never paths, so approving it there would
# approve every command anywhere.
#
# The two gates are sequential, not additive. A worker only asks acpx about
# actions it did not settle itself, so in "auto" mode the policy above becomes
# the backstop for whatever the classifier escalates rather than the first and
# only word. Set "default" to restore prompt-everything behaviour.
DEFAULT_PERMISSION_MODE = "auto"


class ConfigurationError(Exception):
    """A request or a setting this plugin refuses to guess at.

    ``error_type`` separates faults the caller can fix by retrying differently
    from faults only the operator can fix, because the two need different
    responses and look identical in the message alone.
    """

    def __init__(self, message: str, error_type: str = "invalid_request"):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class Settings:
    allowed_cwd_roots: List[str]
    acpx_bin: str = "acpx"
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS
    max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS
    kind_policy: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_KIND_POLICY))
    project_markers: List[str] = field(default_factory=lambda: list(DEFAULT_PROJECT_MARKERS))
    permission_mode: str = DEFAULT_PERMISSION_MODE

    def clamp_timeout(self, requested: Optional[int]) -> int:
        if not requested:
            return self.default_timeout_seconds
        return max(MIN_TIMEOUT_SECONDS, min(int(requested), self.max_timeout_seconds))


def load_settings(hermes_config: Optional[Dict[str, Any]]) -> Settings:
    """Build settings from an already-loaded Hermes config dictionary.

    Raises ``ConfigurationError`` when ``allowed_cwd_roots`` is missing. Failing
    closed matters here: the fallback would be letting a worker edit any path on
    the box, which is precisely what the operator has not yet agreed to.
    """
    entry = _plugin_entry(hermes_config)
    roots = _clean_roots(entry.get("allowed_cwd_roots"))
    if not roots:
        raise ConfigurationError(
            "acp_delegation is not configured. Set "
            "plugins.entries.{}.allowed_cwd_roots in config.yaml to the "
            "directories a delegated worker may operate in.".format(PLUGIN_ID),
            "not_configured",
        )

    return Settings(
        allowed_cwd_roots=roots,
        acpx_bin=entry.get("acpx_bin") or "acpx",
        default_timeout_seconds=_positive_int(
            entry.get("default_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS
        ),
        max_timeout_seconds=_positive_int(entry.get("max_timeout_seconds"), MAX_TIMEOUT_SECONDS),
        max_response_chars=_positive_int(
            entry.get("max_response_chars"), DEFAULT_MAX_RESPONSE_CHARS
        ),
        kind_policy=entry.get("kind_policy") or dict(DEFAULT_KIND_POLICY),
        project_markers=_clean_markers(entry.get("project_markers")),
        permission_mode=(entry.get("permission_mode") or DEFAULT_PERMISSION_MODE).strip(),
    )


def resolve_working_directory(
    raw_path: str,
    allowed_roots: List[str],
    project_markers: Optional[List[str]] = None,
) -> str:
    """Return the project root the worker should run in, or refuse and say why.

    Two separate questions, answered in order, because conflating them is what
    made a directory full of projects look like a valid working directory:

    1. *May* the worker touch this path at all? Admission against the configured
       roots. Symlinks are resolved first — comparing the raw path would let a
       link inside an allowed root point anywhere on the filesystem.
    2. *Which project* is it in? The answer is derived per request rather than
       configured, because one allowed root normally holds many checkouts and
       each delegation targets a different one.

    Step 2 walks up from the requested path, so a subdirectory resolves to its
    project rather than being used as-is. It never walks above the root that
    admitted the path, so anchoring cannot escape the boundary step 1 enforced.
    """
    if not raw_path or not raw_path.strip():
        raise ConfigurationError("cwd is required.", "invalid_cwd")

    resolved = os.path.realpath(os.path.expanduser(raw_path.strip()))
    if not os.path.isdir(resolved):
        raise ConfigurationError(
            "cwd does not exist or is not a directory: {}".format(resolved), "invalid_cwd"
        )

    boundary = _admitting_root(resolved, allowed_roots)
    if boundary is None:
        raise ConfigurationError(
            "cwd {} is outside the allowed roots ({}).".format(resolved, ", ".join(allowed_roots)),
            "invalid_cwd",
        )

    markers = project_markers or list(DEFAULT_PROJECT_MARKERS)
    project_root = _project_root(resolved, boundary, markers)
    if project_root is None:
        raise ConfigurationError(
            "cwd {} is not inside a project: no {} was found between it and the allowed root "
            "{}. Pass the path of one project checkout, not a directory that contains "
            "several.".format(resolved, " or ".join(markers), boundary),
            "invalid_cwd",
        )
    return project_root


def _admitting_root(path: str, roots: List[str]) -> Optional[str]:
    """Return the configured root that contains this path, or None.

    Returns the root rather than a boolean because the anchoring walk needs
    somewhere to stop.
    """
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return root
    return None


def _project_root(path: str, boundary: str, markers: List[str]) -> Optional[str]:
    """Nearest ancestor of ``path`` that looks like a project, ``boundary`` included.

    Including the boundary is what lets an operator configure a single checkout
    as the only allowed root. Excluding everything above it is what stops a
    ``working-repos`` that happens to sit inside some larger repository from
    anchoring there.
    """
    current = path
    while True:
        if _is_project(current, markers):
            return current
        if current == boundary:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _is_project(path: str, markers: List[str]) -> bool:
    return any(os.path.exists(os.path.join(path, marker)) for marker in markers)


def _plugin_entry(hermes_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = hermes_config or {}
    entries = (config.get("plugins") or {}).get("entries") or {}
    return entries.get(PLUGIN_ID) or {}


def _clean_roots(raw_roots: Any) -> List[str]:
    if not isinstance(raw_roots, list):
        return []
    roots = []
    for root in raw_roots:
        if isinstance(root, str) and root.strip():
            roots.append(os.path.realpath(os.path.expanduser(root.strip())))
    return roots


def _clean_markers(raw_markers: Any) -> List[str]:
    """Operator-supplied project markers, falling back to the version-control roots.

    A marker containing a path separator is dropped rather than honoured: these
    are compared against directory entries, and `a/b` would silently never match.
    """
    if not isinstance(raw_markers, list):
        return list(DEFAULT_PROJECT_MARKERS)
    markers = [
        marker.strip()
        for marker in raw_markers
        if isinstance(marker, str) and marker.strip() and os.sep not in marker
    ]
    return markers or list(DEFAULT_PROJECT_MARKERS)


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
