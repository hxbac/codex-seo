#!/usr/bin/env python3
"""Load API credentials from a .env file so they never have to be typed on a command line.

Search order, first readable file wins:

    1. $AI_CONTENT_ENV_FILE                  agent neutral override; empty string disables loading
    2. $CLAUDE_ENV_FILE                      legacy override; empty string disables loading
    3. ~/.config/ai-content/credentials.env  new canonical location, shared across agents
    4. ~/.claude/.env                        shared by every installed plugin, survives a reinstall
    5. <repo root>/.env                      development checkout only

A variable already present in the real environment always wins over the file, so
`export FOO=bar` still overrides it and test runs stay deterministic.

Import for the side effect, at the top of any script that reads credentials:

    import env_file  # noqa: F401

Parsing is deliberately literal. There is no shell expansion, no command
substitution, and no inline comment stripping: everything after the first `=`
is the value. That keeps passwords containing `#`, `$` or a space intact. Wrap a
value in quotes only when you want leading or trailing whitespace removed.

Key rotation
------------
A quota bearing service can define more than one credential slot for the same
variable name. Slot 1 is the plain name. Slot 2 onward appends `_2`, `_3`, and
so on. See CREDENTIAL_GROUPS below for the services this module knows about,
and `rotate()` for how a caller asks for the next working slot when the
current one is rejected or out of quota.

CLI:
    python3 env_file.py --check     report which variables and slots are set, values masked
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, TypeVar

#: Variables this toolchain reads. Used by --check; loading is not limited to them.
KNOWN_VARS = (
    "DATAFORSEO_USERNAME",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "GOOGLE_AI_API_KEY",
    "UNSPLASH_ACCESS_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "NANOBANANA_MODEL",
    "GITHUB_TOKEN",
    "MOZ_API_KEY",
    "BING_WEBMASTER_API_KEY",
    "INDEXNOW_KEY",
    "INDEXNOW_KEY_LOCATION",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GA4_PROPERTY_ID",
    "GSC_PROPERTY",
)

#: Variables whose value must never be echoed, even partially, by --check.
SECRET_SUFFIXES = ("PASSWORD", "TOKEN", "SECRET", "KEY")

#: Rotatable credential groups: group name to the canonical member variable
#: names that make up one slot. A slot is complete only when every member is
#: present for that slot's index.
CREDENTIAL_GROUPS = {
    "dataforseo": ("DATAFORSEO_USERNAME", "DATAFORSEO_PASSWORD"),
    "pexels": ("PEXELS_API_KEY",),
    "pixabay": ("PIXABAY_API_KEY",),
    "unsplash": ("UNSPLASH_ACCESS_KEY",),
    "moz": ("MOZ_API_KEY",),
    "google_ai": ("GOOGLE_AI_API_KEY",),
    "firecrawl": ("FIRECRAWL_API_KEY",),
}

#: A canonical member name to the other names accepted in its place, checked
#: in order after the canonical name itself. Kept for the historical split
#: between claude-blog and claude-seo over DATAFORSEO_USERNAME versus
#: DATAFORSEO_LOGIN.
ALIASES: Dict[str, tuple] = {
    "DATAFORSEO_USERNAME": ("DATAFORSEO_LOGIN",),
}

#: Enumeration stops here even if the environment defines more, to bound a
#: malformed file.
MAX_SLOTS = 20

#: Seconds a rejected slot is skipped for, unless every slot is cooling.
#: Override with AI_CONTENT_KEY_COOLDOWN.
DEFAULT_COOLDOWN_SECONDS = 1800

#: HTTP status codes that mean the credential itself is the problem, not the
#: request. Used by the default rotate() classifier.
ROTATABLE_HTTP_STATUS = (401, 402, 403, 429)

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_loaded_from: Optional[Path] = None
_loaded_names: List[str] = []

T = TypeVar("T")


class Slot(NamedTuple):
    """One usable credential slot for a group.

    `index` is 1 based. `values` maps each canonical member name (without the
    numeric suffix) to the resolved value for this slot.
    """

    index: int
    values: Dict[str, str]


class CredentialsMissing(Exception):
    """No usable slot exists at all for a credential group."""


class AllSlotsFailed(Exception):
    """Every configured slot for a group was tried and rejected.

    The exception that ended the last attempt is chained as `__cause__`.
    """


class RotatableError(Exception):
    """Raise from inside a `rotate()` call to force a slot switch.

    Use this for a protocol level failure that an HTTP status code cannot
    express, such as a DataForSEO task whose `tasks[0].status_code` falls in
    the 40100 to 40399 range even though the HTTP response itself was 200.
    """


def parse(text: str) -> Dict[str, str]:
    """Turn the text of a .env file into a mapping. Malformed lines are skipped."""
    result: Dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace("\\n", "\n").replace("\\t", "\t")
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        result[key] = value
    return result


def _override_paths(name: str) -> Optional[List[Path]]:
    """Resolve one override env var into a candidate list, or None if unset.

    An empty (or whitespace only) value is an explicit opt out: it returns an
    empty list rather than None, so the caller stops looking further.
    """
    override = os.environ.get(name)
    if override is None:
        return None
    return [] if not override.strip() else [Path(override).expanduser()]


def candidate_paths() -> List[Path]:
    """Every location searched, in priority order."""
    for name in ("AI_CONTENT_ENV_FILE", "CLAUDE_ENV_FILE"):
        found = _override_paths(name)
        if found is not None:
            return found
    paths = [
        Path.home() / ".config" / "ai-content" / "credentials.env",
        Path.home() / ".claude" / ".env",
    ]
    repo_root = Path(__file__).resolve().parent.parent
    paths.append(repo_root / ".env")
    return paths


def find_env_file() -> Optional[Path]:
    """First candidate that exists and is readable, or None."""
    for path in candidate_paths():
        try:
            if path.is_file() and os.access(path, os.R_OK):
                return path
        except OSError:
            continue
    return None


def _warn_on_loose_permissions(path: Path) -> None:
    """Credentials in a group or world readable file are a real exposure."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        sys.stderr.write(
            "warning: {0} is readable by other accounts (mode {1:04o}). "
            "Run: chmod 600 {0}\n".format(path, mode)
        )


def load(path: Optional[Path] = None, *, override: bool = False) -> List[str]:
    """Copy the file's variables into os.environ. Returns the names applied.

    Values are never returned or logged. An existing environment variable is
    kept unless `override` is true.
    """
    global _loaded_from, _loaded_names
    target = path or find_env_file()
    if target is None:
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    _warn_on_loose_permissions(target)
    applied: List[str] = []
    for key, value in parse(text).items():
        if override or not os.environ.get(key):
            os.environ[key] = value
            applied.append(key)
    _loaded_from = target
    _loaded_names = applied
    return applied


def source() -> Optional[Path]:
    """The file the current process loaded, or None."""
    return _loaded_from


def _mask(name: str, value: str) -> str:
    if any(name.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return "set ({0} chars)".format(len(value))
    return value


# ---------------------------------------------------------------------------
# Credential slots
# ---------------------------------------------------------------------------


def _member_value(member: str, suffix: str) -> str:
    """Resolve one member variable for one slot suffix, checking aliases."""
    for candidate in (member,) + ALIASES.get(member, ()):
        value = os.environ.get(candidate + suffix)
        if value:
            return value
    return ""


def _slot_suffix(index: int) -> str:
    return "" if index == 1 else "_{0}".format(index)


def _any_member_present(members: tuple, index: int) -> bool:
    suffix = _slot_suffix(index)
    return any(_member_value(member, suffix) for member in members)


def slots(group: str) -> List[Slot]:
    """Enumerate the usable, complete slots configured for `group`.

    Slot 1 is the base variable name(s). Slot N (N >= 2) is each member with
    a numeric `_N` suffix. Enumeration walks contiguous indexes starting at
    1 and stops at the first index that defines none of the group's members,
    provided a later index does define at least one of them (a real gap); it
    also stops silently, without a warning, once the environment simply has
    nothing further to offer. It never looks past MAX_SLOTS.

    A slot missing only some of its members is incomplete: it is skipped (not
    counted as a returned slot) and one warning is written to stderr naming
    it, but enumeration continues past it.
    """
    members = CREDENTIAL_GROUPS.get(group)
    if not members:
        return []
    result: List[Slot] = []
    for index in range(1, MAX_SLOTS + 1):
        suffix = _slot_suffix(index)
        values: Dict[str, str] = {}
        missing = []
        for member in members:
            value = _member_value(member, suffix)
            if value:
                values[member] = value
            else:
                missing.append(member)
        if not values:
            has_more = any(
                _any_member_present(members, later)
                for later in range(index + 1, MAX_SLOTS + 1)
            )
            if has_more:
                sys.stderr.write(
                    "warning: {0} slot {1} is not defined but a later slot is; "
                    "stopping enumeration at the gap.\n".format(group, index)
                )
            break
        if missing:
            sys.stderr.write(
                "warning: {0} slot {1} is incomplete (missing {2}); skipping it.\n".format(
                    group, index, ", ".join(missing)
                )
            )
            continue
        result.append(Slot(index=index, values=values))
    return result


# ---------------------------------------------------------------------------
# Cooldown state
# ---------------------------------------------------------------------------

def _cooldown_seconds() -> int:
    raw = os.environ.get("AI_CONTENT_KEY_COOLDOWN")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_COOLDOWN_SECONDS


def _state_disabled() -> bool:
    """True when the test isolation switch has emptied either override.

    A file discovery override set to the empty string means loading is off
    for the whole process; the cooldown state file follows the same switch so
    a test run never touches a real machine's credential state.
    """
    for name in ("AI_CONTENT_ENV_FILE", "CLAUDE_ENV_FILE"):
        value = os.environ.get(name)
        if value is not None and not value.strip():
            return True
    return False


def state_file_path() -> Optional[Path]:
    """Where slot cooldowns are recorded, or None while state is disabled."""
    if _state_disabled():
        return None
    return Path.home() / ".config" / "ai-content" / "keyring-state.json"


def _state_scope() -> str:
    """Which credentials file the recorded cooldowns belong to.

    Cooldowns must not cross credential sets. Keying the state by group and
    slot index alone lets a run against one credentials file suppress a slot
    in a completely different one, because slot 2 of file A and slot 2 of
    file B share a record. Anyone testing a second key set, or working across
    two projects, silently loses a good key for the cooldown period.

    The resolved file path is the scope. It carries no secret: the values
    inside the file never reach this state file.
    """
    path = find_env_file()
    return str(path) if path is not None else "(no credentials file)"


def _load_state() -> dict:
    """Return the cooldown records for the credentials file currently in use."""
    return _load_state_all().get(_state_scope(), {})


def _load_state_all() -> dict:
    path = state_file_path()
    if path is None:
        return {}
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("keyring state file did not contain a JSON object")
        return data
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "warning: keyring state file {0} is corrupt or unreadable ({1}); "
            "treating it as empty.\n".format(path, exc)
        )
        return {}


def _save_state(data: dict) -> None:
    """Persist `data` as the cooldown records for the credentials file in use."""
    everything = _load_state_all()
    # Pre-scope state files were a flat {group: {slot: record}} map. Those
    # records cannot be attributed to any credentials file, and a cooldown is
    # ephemeral by nature, so drop them rather than guess an owner.
    everything = {
        key: value
        for key, value in everything.items()
        if isinstance(value, dict) and (key.startswith("/") or key.startswith("("))
    }
    if data:
        everything[_state_scope()] = data
    else:
        everything.pop(_state_scope(), None)
    _save_state_all(everything)


def _save_state_all(data: dict) -> None:
    path = state_file_path()
    if path is None:
        return
    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".keyring-state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, str(path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        # A machine with an unwritable home directory should not crash the
        # caller over a cooldown record. It just retries slots more often.
        return


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def report_slot_failure(group: str, index: int, reason: str) -> None:
    """Record that `group` slot `index` was just rejected, for cooldown."""
    if state_file_path() is None:
        return
    data = _load_state()
    group_data = data.setdefault(group, {})
    group_data[str(index)] = {"failed_at": _now_iso(), "reason": reason}
    _save_state(data)


def _clear_slot_failure(group: str, index: int) -> None:
    if state_file_path() is None:
        return
    data = _load_state()
    group_data = data.get(group)
    if group_data and str(index) in group_data:
        del group_data[str(index)]
        if not group_data:
            del data[group]
        _save_state(data)


def _remaining_cooldown_seconds(group: str, index: int) -> Optional[float]:
    data = _load_state()
    entry = data.get(group, {}).get(str(index))
    if not entry:
        return None
    failed_at = _parse_iso(entry.get("failed_at"))
    if failed_at is None:
        return None
    elapsed = (_now() - failed_at).total_seconds()
    remaining = _cooldown_seconds() - elapsed
    return remaining if remaining > 0 else None


def slot_is_cooling(group: str, index: int) -> bool:
    """True if `group` slot `index` failed recently enough to still be skipped."""
    if state_file_path() is None:
        return False
    return _remaining_cooldown_seconds(group, index) is not None


def _least_recently_failed(group: str, candidates: List[Slot]) -> Slot:
    data = _load_state()
    group_data = data.get(group, {})

    def failed_at(slot: Slot) -> datetime:
        entry = group_data.get(str(slot.index), {})
        return _parse_iso(entry.get("failed_at")) or _now()

    return min(candidates, key=failed_at)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def _reason_for(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP {0}".format(exc.code)
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _default_classify(exc: BaseException) -> bool:
    if isinstance(exc, RotatableError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in ROTATABLE_HTTP_STATUS
    return False


def rotate(
    group: str,
    call: Callable[[Slot], T],
    *,
    classify: Optional[Callable[[BaseException], bool]] = None,
) -> T:
    """Call `call(slot)` with a working slot from `group`, switching on rejection.

    Slots currently in cooldown are skipped. If every slot is cooling, the
    least recently failed one has its cooldown cleared and is tried anyway,
    so the caller is never left with nothing to try.

    `call` raising an exception that `classify` (default: HTTP 401, 402, 403,
    429, or RotatableError) accepts as rotatable moves on to the next slot,
    after recording the failure and printing a one line notice to stderr.
    Any other exception propagates immediately without touching later slots.

    Raises CredentialsMissing when the group has no complete slot at all, and
    AllSlotsFailed, chaining the last exception, when every slot was tried
    and none worked.
    """
    classify = classify or _default_classify
    all_slots = slots(group)
    if not all_slots:
        raise CredentialsMissing("no credentials configured for group: {0}".format(group))

    ready = [slot for slot in all_slots if not slot_is_cooling(group, slot.index)]
    if not ready:
        least = _least_recently_failed(group, all_slots)
        sys.stderr.write(
            "[keyring] {0} all {1} slots are cooling. Retrying slot {2} anyway.\n".format(
                group, len(all_slots), least.index
            )
        )
        _clear_slot_failure(group, least.index)
        candidates = [least] + [slot for slot in all_slots if slot.index != least.index]
    else:
        skipped = [slot for slot in all_slots if slot not in ready]
        if skipped:
            first_skipped = skipped[0]
            remaining = _remaining_cooldown_seconds(group, first_skipped.index) or 0.0
            minutes = int(remaining // 60) + (1 if remaining % 60 else 0)
            sys.stderr.write(
                "[keyring] {0} slot {1} is in cooldown for another {2} min. "
                "Starting at slot {3}.\n".format(group, first_skipped.index, minutes, ready[0].index)
            )
        candidates = ready

    total = len(all_slots)
    last_exc: Optional[BaseException] = None
    for position, slot in enumerate(candidates):
        try:
            result = call(slot)
        except BaseException as exc:  # noqa: BLE001 - re-raised below when not rotatable
            if not classify(exc):
                raise
            reason = _reason_for(exc)
            report_slot_failure(group, slot.index, reason)
            last_exc = exc
            if position + 1 < len(candidates):
                next_slot = candidates[position + 1]
                sys.stderr.write(
                    "[keyring] {0} slot {1} rejected ({2}). Switching to slot {3} of {4}.\n".format(
                        group, slot.index, reason, next_slot.index, total
                    )
                )
            continue
        else:
            _clear_slot_failure(group, slot.index)
            return result

    reason = _reason_for(last_exc) if last_exc is not None else "unknown"
    sys.stderr.write(
        "[keyring] {0}: all {1} slots failed. Last error: {2}. "
        "Add another key or top up the account.\n".format(group, total, reason)
    )
    raise AllSlotsFailed(
        "{0}: all {1} slots failed. Last error: {2}".format(group, total, reason)
    ) from last_exc


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def _slot_status_text(group: str, slot: Slot) -> str:
    if slot_is_cooling(group, slot.index):
        data = _load_state()
        entry = data.get(group, {}).get(str(slot.index), {})
        reason = entry.get("reason", "unknown")
        remaining = _remaining_cooldown_seconds(group, slot.index) or 0.0
        minutes = int(remaining // 60) + (1 if remaining % 60 else 0)
        return "slot {0} cooling ({1}, {2} min left)".format(slot.index, reason, minutes)
    return "slot {0} ready".format(slot.index)


def _group_summary_line(group: str) -> str:
    group_slots = slots(group)
    count = len(group_slots)
    label = "{0} slot{1}".format(count, "" if count == 1 else "s")
    if count == 0:
        status = "not configured"
    else:
        status = ", ".join(_slot_status_text(group, slot) for slot in group_slots)
    return "  {0:14s}{1:10s}{2}".format(group, label, status)


def _check() -> int:
    found = source()
    if found is None:
        searched = ", ".join(str(p) for p in candidate_paths()) or "nothing (loading is disabled)"
        print("env file: none found")
        print("searched: " + searched)
    else:
        print("env file: {0}".format(found))
    print("")
    for group in CREDENTIAL_GROUPS:
        print(_group_summary_line(group))
    print("")
    missing = []
    for name in KNOWN_VARS:
        value = os.environ.get(name)
        if value:
            print("  OK      {0:32s} {1}".format(name, _mask(name, value)))
        else:
            missing.append(name)
    for name in missing:
        print("  unset   {0}".format(name))
    return 0


load()


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(_check())
    print(__doc__)
