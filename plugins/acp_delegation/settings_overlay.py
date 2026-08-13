"""Path-level permissions, installed into the worker's own settings file.

acpx decides permissions by tool *kind*, never by path, so approving the ``edit``
kind approves an edit anywhere on the filesystem. Claude Code's own permission
rules do understand paths, and the ACP session loads project-scoped settings
(``settingSources: ["user", "project", "local"]``, rooted at the cwd acpx is
given), so the deny globs go there instead.

The overlay is written to ``<project root>/.claude/settings.local.json`` — the
conventionally gitignored scope — and removed again when the run ends. The shared
``~/.claude/settings.json`` is deliberately untouched: the operator's own
interactive sessions read it.

**The file belongs to the human, not to this plugin.** It is a checkout they also
work in, so every write here is reversible and every rule this plugin adds is
labelled with who added it. That is why restore works by *marker* rather than by
snapshot. Snapshot restore assumes one writer and an uninterrupted process, and
holds only while both are true:

- Killed mid-run — a gateway bounce, a reboot — the snapshot dies with the
  process and the deny rules stay in the operator's repository forever, silently
  applying to their own interactive sessions. Marker restore self-heals instead:
  the next install in that directory prunes whatever a dead run left behind.
- Two delegations in one checkout — the second reads the first's overlay as
  "previous", and restoring in either order leaves residue or disarms a run that
  is still going. Here every run records the rules it *requires*, and a rule is
  withdrawn only once no remaining run requires it.

The marker therefore separates two things that look alike in the deny list:
``holders`` is who currently needs a rule, and ``owned`` is which rules this
plugin introduced. A rule the repository denies on its own never enters
``owned``, so no restore can ever take it away.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

SETTINGS_DIRECTORY = ".claude"
SETTINGS_FILENAME = "settings.local.json"

# Where this plugin records what it added. A reserved key rather than a comment
# so it survives a round trip through json, and a greppable one so a leaked
# overlay can be found by name across every checkout.
OVERLAY_KEY = "_acp_delegation"


@dataclass
class OverlayHandle:
    """What is needed to withdraw this run's rules and nobody else's."""

    path: str
    run_id: str
    created_directory: bool


def install(
    working_directory: str, deny_rules: List[str], run_id: str
) -> Optional[OverlayHandle]:
    """Add this run's deny rules to the project's local settings.

    Returns None when there is nothing to install, so the caller can skip the
    restore. Rules the project already denies are honoured but never claimed, so
    no restore can withdraw a rule the operator wrote.
    """
    if not deny_rules:
        return None

    settings_path = _settings_path(working_directory)
    created_directory = _ensure_directory(os.path.dirname(settings_path))

    settings = _prune_dead_holders(_parse(_read_text(settings_path)))
    settings = _add_holder(settings, run_id, deny_rules)
    _write_text(settings_path, json.dumps(settings, indent=2) + "\n")

    return OverlayHandle(
        path=settings_path,
        run_id=run_id,
        created_directory=created_directory,
    )


def restore(handle: Optional[OverlayHandle]) -> None:
    """Withdraw this run's rules. Safe to call twice, and after a failure.

    Re-reads the file rather than restoring a snapshot of it, so edits made
    while the worker ran — by the operator, or by a concurrent delegation — are
    kept.
    """
    if handle is None:
        return

    settings = _remove_holder(_parse(_read_text(handle.path)), handle.run_id)

    if settings:
        _write_text(handle.path, json.dumps(settings, indent=2) + "\n")
        return

    # An empty settings file configures nothing, so it is residue whoever wrote
    # it. Deleting on emptiness rather than on "did this run create the file"
    # is what makes the *second* of two concurrent runs clean up: by then the
    # file was created by the first, and neither would ever own the removal.
    _remove_quietly(handle.path)
    if handle.created_directory:
        _remove_directory_if_empty(os.path.dirname(handle.path))


def _add_holder(settings: Dict[str, Any], run_id: str, deny_rules: List[str]) -> Dict[str, Any]:
    """Record this run as requiring these rules, and add any that are missing."""
    existing = _deny_list(settings)
    owned = _owned(settings)
    introduced = [rule for rule in deny_rules if rule not in existing and rule not in owned]

    merged = _with_deny(settings, existing + [rule for rule in deny_rules if rule not in existing])
    merged[OVERLAY_KEY] = {
        "holders": _holders(settings)
        + [{"run": run_id, "pid": os.getpid(), "rules": list(deny_rules)}],
        "owned": owned + introduced,
    }
    return merged


def _remove_holder(settings: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    mine = [holder for holder in _holders(settings) if holder.get("run") == run_id]
    return _drop_holders(settings, mine)


def _prune_dead_holders(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Withdraw rules left by a run whose process is gone.

    This is the self-heal for a delegation that was killed before it could
    restore. It runs on install rather than on a timer because a directory
    nobody delegates into again is a directory where a leaked overlay is doing
    no harm — and OVERLAY_KEY makes it findable by hand either way.

    A pid can be reused, so this is a heuristic. Both errors are mild: a
    resurrected pid keeps rules a little longer, and a false negative only
    withdraws rules from a run that has already stopped.
    """
    dead = [holder for holder in _holders(settings) if not _is_running(holder.get("pid"))]
    return _drop_holders(settings, dead)


def _drop_holders(settings: Dict[str, Any], doomed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Remove these holders, and any rule no surviving holder still requires.

    The two-step is the whole point. Withdrawing whatever the departing run
    listed would disarm a concurrent run that requires the same rules — and it
    requires exactly the same rules, because both get the same denylist.
    """
    if not doomed:
        return dict(settings)

    remaining = [holder for holder in _holders(settings) if holder not in doomed]
    still_required = {rule for holder in remaining for rule in (holder.get("rules") or [])}

    owned = _owned(settings)
    withdrawn = [rule for rule in owned if rule not in still_required]

    result = _with_deny(settings, [rule for rule in _deny_list(settings) if rule not in withdrawn])
    if remaining:
        result[OVERLAY_KEY] = {
            "holders": remaining,
            "owned": [rule for rule in owned if rule in still_required],
        }
    else:
        result.pop(OVERLAY_KEY, None)
    return result


def _holders(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [holder for holder in _overlay(settings, "holders") if isinstance(holder, dict)]


def _owned(settings: Dict[str, Any]) -> List[str]:
    return [rule for rule in _overlay(settings, "owned") if isinstance(rule, str)]


def _overlay(settings: Dict[str, Any], key: str) -> List[Any]:
    overlay = settings.get(OVERLAY_KEY)
    if not isinstance(overlay, dict):
        return []
    value = overlay.get(key)
    return value if isinstance(value, list) else []


def _deny_list(settings: Dict[str, Any]) -> List[str]:
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return []
    deny = permissions.get("deny")
    return list(deny) if isinstance(deny, list) else []


def _with_deny(settings: Dict[str, Any], deny: List[str]) -> Dict[str, Any]:
    """Return a copy carrying this deny list, dropping the keys it empties.

    An empty `permissions.deny` this plugin created is residue, not settings, and
    the operator would have to decide whether it meant anything.
    """
    result = dict(settings)
    permissions = dict(result.get("permissions") or {})

    if deny:
        permissions["deny"] = deny
    else:
        permissions.pop("deny", None)

    if permissions:
        result["permissions"] = permissions
    else:
        result.pop("permissions", None)
    return result


def _is_running(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, and owned by somebody else.
        return True
    except OSError:
        return False
    return True


def _settings_path(working_directory: str) -> str:
    return os.path.join(working_directory, SETTINGS_DIRECTORY, SETTINGS_FILENAME)


def _parse(content: Optional[str]) -> Dict[str, Any]:
    """Read existing settings, treating unreadable content as absent.

    A malformed settings file must not stop a delegation. It does mean this run
    cannot see the project's own rules, so it re-adds every one of its own — and
    withdraws exactly those again on restore.
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
