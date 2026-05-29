# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""terminatorlib.criu.session - hidden auto-saved session.

The user-facing Layouts feature is preserved unchanged — those remain
explicit workflow templates loaded via `--layout=NAME`.

Separately from that, we maintain a hidden "session" — the arrangement
of windows/tabs/splits at the last point the user did something that
implies "I want to come back to this": clicking "Checkpoint this tab",
closing the whole window with tabs in it, OS shutdown, an external
checkpoint kick, etc.

On terminator startup, if no `--layout=NAME` was given AND a session
file exists, we load the session instead of the configured default
layout. The session is JSON instead of terminator's configobj format
so it's clearly distinct from user layouts and never appears in the
Preferences → Layouts UI.

Path: `$XDG_DATA_HOME/terminator-criu/session.json`.
"""

import json
import os
import uuid as _uuid_module


def _xdg_data_home():
    return os.environ.get("XDG_DATA_HOME") or \
        os.path.expanduser("~/.local/share")


def session_path():
    return os.path.join(_xdg_data_home(), "terminator-criu", "session.json")


def _coerce_for_json(value):
    """Layout dicts hold a mix of native types and UUID objects. Make
    everything JSON-safe (UUIDs become their hex string)."""
    if isinstance(value, _uuid_module.UUID):
        return value.hex
    if isinstance(value, dict):
        return {k: _coerce_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_for_json(v) for v in value]
    return value


def save(layout):
    """Persist `layout` (the flat dict from Terminator.describe_layout)
    to the session file. Created with mode 0644 in case the user wants
    to inspect or edit it manually.

    Refuses to overwrite an existing session with an empty layout —
    the late-firing Gtk destroy path used to call here with `{}` and
    clobber a perfectly good save from earlier.
    """
    if isinstance(layout, dict) and not layout:
        if os.path.exists(session_path()):
            return  # don't clobber prior content with nothing
    path = session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    payload = _coerce_for_json(layout)
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load():
    """Return the session layout dict or None if no session exists."""
    path = session_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear():
    """Remove the session file (used after a clean shutdown or when
    the user explicitly opts out of restore)."""
    try:
        os.unlink(session_path())
    except OSError:
        pass


def _checkpoints_root():
    return os.path.join(_xdg_data_home(), "terminator-criu", "checkpoints")


def referenced_uuids(layout):
    """Extract the hex-string set of UUIDs flagged criu_restore=true
    in a layout dict. Values stored as hex (no dashes) to match the
    checkpoint dir naming convention."""
    out = set()
    if not isinstance(layout, dict):
        return out
    for entry in layout.values():
        if not isinstance(entry, dict):
            continue
        if not entry.get("criu_restore"):
            continue
        u = entry.get("uuid", "")
        if not u:
            continue
        out.add(str(u).replace("-", "").lower())
    return out


def remove_orphan_checkpoints(keep_uuids):
    """Delete checkpoint dirs whose UUID is NOT in `keep_uuids`.

    Run on startup after consuming a session — any dir not referenced
    by the just-loaded session is an orphan (terminator crashed in a
    weird way, or an external process created garbage, etc.). Removing
    them keeps the checkpoints/ dir hygienic. No-op if the ckpts dir
    doesn't exist.
    """
    import shutil
    root = _checkpoints_root()
    if not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        if name in keep_uuids:
            continue
        full = os.path.join(root, name)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
            removed += 1
    return removed
