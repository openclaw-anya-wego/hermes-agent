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

# Kind-level permissions. acpx matches tool kinds and names, never paths, so
# path scoping is the settings-overlay layer's job — see settings_overlay.py.
DEFAULT_KIND_POLICY: Dict[str, Any] = {
    "autoApprove": ["read", "search", "edit"],
    "autoDeny": [],
    "escalate": [],
    "defaultAction": "deny",
}


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
    )


def resolve_working_directory(raw_path: str, allowed_roots: List[str]) -> str:
    """Return the absolute working directory, or explain why it is refused.

    Symlinks are resolved before the prefix check. Comparing the raw path would
    let a link inside an allowed root point anywhere on the filesystem.
    """
    if not raw_path or not raw_path.strip():
        raise ConfigurationError("cwd is required.", "invalid_cwd")

    resolved = os.path.realpath(os.path.expanduser(raw_path.strip()))
    if not os.path.isdir(resolved):
        raise ConfigurationError(
            "cwd does not exist or is not a directory: {}".format(resolved), "invalid_cwd"
        )

    if not _is_within_any(resolved, allowed_roots):
        raise ConfigurationError(
            "cwd {} is outside the allowed roots ({}).".format(resolved, ", ".join(allowed_roots)),
            "invalid_cwd",
        )
    return resolved


def _is_within_any(path: str, roots: List[str]) -> bool:
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


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


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
