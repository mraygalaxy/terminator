# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""terminatorlib.criu.inspect - best-effort recovery from a checkpoint's
own dump images when session.json is missing or incomplete.

Normally the working directory and command line to relaunch on a failed
restore come from `criu_spawn_argv`/`criu_spawn_cwd` in session.json
(populated at spawn time, in the same process). But session.json can be
absent entirely (deleted before this launch, a crash destroyed it, an
orphaned checkpoint dir with no matching session entry) while a
checkpoint dir with a complete CRIU dump still exists on disk. In that
case CRIU's own images already contain the answer — the dumped
process's cwd and executable path — decoded here via `crit` (CRIU's
image tool, always installed alongside `criu` itself) as a last-resort
fallback so a tab can still come back in the right place doing
something close to what it was doing, rather than dropping to the
profile's bare default shell in $HOME.

Every function here is best-effort: any failure (crit missing, images
malformed, an unsupported CRIU image format bump) returns None rather
than raising, since this is a fallback bolted onto an already-degraded
path — it must never introduce a new way to crash or block startup.
"""
import json
import os
import subprocess


def _crit_decode(img_path):
    """Decode one CRIU image file to a dict via `crit decode`. Returns
    None on any failure (missing binary, bad path, corrupt image)."""
    try:
        proc = subprocess.run(
            ["crit", "decode", "-i", img_path],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _pids_in_dump(ckpt_dir):
    """Return the pids of the root process(es) captured in this dump,
    read from pstree.img (present in every complete CRIU dump)."""
    data = _crit_decode(os.path.join(ckpt_dir, "pstree.img"))
    if not data or "entries" not in data:
        return []
    return [e["pid"] for e in data["entries"] if "pid" in e]


def recover_cwd(ckpt_dir):
    """Best-effort: the working directory of the checkpointed process,
    read directly from the dump's fs-<pid>.img + files.img. Returns
    None if it can't be determined."""
    pids = _pids_in_dump(ckpt_dir)
    if not pids:
        return None
    files_data = _crit_decode(os.path.join(ckpt_dir, "files.img"))
    if not files_data or "entries" not in files_data:
        return None
    by_id = {}
    for entry in files_data["entries"]:
        reg = entry.get("reg")
        if reg and "id" in reg:
            by_id[reg["id"]] = reg.get("name")

    for pid in pids:
        fs_data = _crit_decode(os.path.join(ckpt_dir, "fs-%d.img" % pid))
        if not fs_data or "entries" not in fs_data or not fs_data["entries"]:
            continue
        cwd_id = fs_data["entries"][0].get("cwd_id")
        cwd = by_id.get(cwd_id)
        if cwd:
            return cwd
    return None


def recover_exe(ckpt_dir):
    """Best-effort: the executable path of the checkpointed process,
    read from mm-<pid>.img's exe_file_id + files.img. Returns None if
    it can't be determined.

    This is NOT the original argv — CRIU doesn't store that as a
    dedicated image field (it lives in the target's own dumped memory,
    at mm_arg_start..mm_arg_end, which would need matching against
    pagemap-<pid>.img/pages-N.img to extract reliably) — just the
    program that was running. For an idle shell tab this is just the
    shell itself (not useful beyond what the normal default-shell
    fallback already does); the caller should only act on this when it
    names something other than the tab's own default shell.
    """
    pids = _pids_in_dump(ckpt_dir)
    if not pids:
        return None
    files_data = _crit_decode(os.path.join(ckpt_dir, "files.img"))
    if not files_data or "entries" not in files_data:
        return None
    by_id = {}
    for entry in files_data["entries"]:
        reg = entry.get("reg")
        if reg and "id" in reg:
            by_id[reg["id"]] = reg.get("name")

    for pid in pids:
        mm_data = _crit_decode(os.path.join(ckpt_dir, "mm-%d.img" % pid))
        if not mm_data or "entries" not in mm_data or not mm_data["entries"]:
            continue
        exe_id = mm_data["entries"][0].get("exe_file_id")
        exe = by_id.get(exe_id)
        if exe:
            return exe
    return None
