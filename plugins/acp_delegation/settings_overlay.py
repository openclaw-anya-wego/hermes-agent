"""Path-level permissions, installed into the worker's own settings file.

acpx decides permissions by tool *kind*, never by path, so approving the ``edit``
kind approves an edit anywhere on the filesystem. Claude Code's own permission
rules do understand paths, and the ACP session loads project-scoped settings, so
the deny globs go there instead.

The overlay is written to ``<cwd>/.claude/settings.local.json`` — the
conventionally gitignored scope — and removed again when the run ends. The
shared ``~/.claude/settings.json`` is deliberately untouched: the operator's own
interactive sessions read it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

SETTINGS_DIRECTORY = ".claude"
SETTINGS_FILENAME = "settings.local.json"


@dataclass
class OverlayHandle:
    """What is needed to put the working tree back exactly as it was."""

    path: str
    previous_content: Optional[str]
    created_directory: bool


def install(working_directory: str, deny_rules: List[str]) -> Optional[OverlayHandle]:
    """Merge deny rules into the working directory's local settings.

    Returns None when there is nothing to install, so the caller can skip the
    restore. Existing settings are preserved: the deny list is unioned rather
    than replaced, because a repository may already deny paths of its own.
    """
    if not deny_rules:
        return None

    settings_path = _settings_path(working_directory)
    created_directory = _ensure_directory(os.path.dirname(settings_path))
    previous_content = _read_text(settings_path)

    merged = _merge_deny_rules(_parse(previous_content), deny_rules)
    _write_text(settings_path, json.dumps(merged, indent=2) + "\n")

    return OverlayHandle(
        path=settings_path,
        previous_content=previous_content,
        created_directory=created_directory,
    )


def restore(handle: Optional[OverlayHandle]) -> None:
    """Undo install(). Safe to call twice and safe to call after a failure."""
    if handle is None:
        return

    if handle.previous_content is None:
        _remove_quietly(handle.path)
        if handle.created_directory:
            _remove_directory_if_empty(os.path.dirname(handle.path))
        return

    _write_text(handle.path, handle.previous_content)


def _merge_deny_rules(settings: Dict[str, Any], deny_rules: List[str]) -> Dict[str, Any]:
    merged = dict(settings)
    permissions = dict(merged.get("permissions") or {})
    existing = permissions.get("deny")
    existing = list(existing) if isinstance(existing, list) else []

    for rule in deny_rules:
        if rule not in existing:
            existing.append(rule)

    permissions["deny"] = existing
    merged["permissions"] = permissions
    return merged


def _settings_path(working_directory: str) -> str:
    return os.path.join(working_directory, SETTINGS_DIRECTORY, SETTINGS_FILENAME)


def _parse(content: Optional[str]) -> Dict[str, Any]:
    """Read existing settings, treating unreadable content as absent.

    A malformed settings file must not stop a delegation: the overlay is
    additive, and the original text is restored verbatim afterwards either way.
    """
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ensure_directory(path: str) -> bool:
    if os.path.isdir(path):
        return False
    os.makedirs(path, exist_ok=True)
    return True


def _read_text(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_directory_if_empty(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass
