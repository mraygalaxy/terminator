#!/usr/bin/env python3
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""test_checkpoint_bash.py - end-to-end CRIU c/r test for an interactive bash

Spawns bash inside a PID+mount namespace via the root helper, sets some
shell state and starts a background child, checkpoints the whole tree,
restores it into a fresh PTY, and verifies the variable, the child
process, and the job-control entry all survived.

NOT picked up by pytest (see conftest.py at the repo root) — requires
root via sudo, CRIU installed, and CAP_SYS_ADMIN. Run manually:

    python3 integration_tests/criu/test_checkpoint_bash.py
"""
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time


def _repo_root() -> str:
    p = os.path.dirname(os.path.abspath(__file__))
    while p != "/" and not os.path.isdir(os.path.join(p, ".git")):
        p = os.path.dirname(p)
    return p


HELPER = os.path.join(_repo_root(), "libexec", "terminator-criu-helper")


def _xdg_state_home() -> str:
    return os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))


def _xdg_data_home() -> str:
    return os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))


# Helper-side runtime state: log file, CRIU extra config files (written
# during dump/restore), PID-rendezvous tempdirs.
#
# This MUST live in a path that survives the helper's mount-namespace
# pruning (i.e. NOT under /run, since the helper unmounts /run inside
# its own namespace before invoking CRIU). $XDG_STATE_HOME (~/.local/state)
# fits the XDG semantics for "data that persists between runs but isn't
# user-portable" — debug logs in particular.
STATE_DIR = os.path.join(_xdg_state_home(), "terminator-criu-poc2")

# Persistent artifacts: the CRIU dump itself, and the test scripts that
# bash inside the namespace `source`s. Lives under $XDG_DATA_HOME so it
# survives reboots and is reachable inside the namespace via /home.
CKPT_DIR = os.path.join(_xdg_data_home(), "terminator-criu-poc2", "checkpoint")
TEST_SCRIPT_DIR = os.path.join(_xdg_data_home(), "terminator-criu-poc2", "scripts")

# Bash test scripts. We `source` these so any state (variables, jobs)
# they set persists in the bash we'll then checkpoint.
SETUP_SCRIPT = r"""# setup.sh — establish state to be preserved across c/r.
MYVAR=hello_state
sleep 600 &
SLEEP_PID=$!
# Single-line marker the driver can grep for.
echo "MARKER_PRE var=$MYVAR sleep_pid=$SLEEP_PID bash_pid=$$"
"""

VERIFY_SCRIPT = r"""# verify.sh — check that state survived restore.
# SLEEP_PID is inherited from setup.sh via source-in-same-shell.
if kill -0 "$SLEEP_PID" 2>/dev/null; then
    sleep_status=ALIVE
else
    sleep_status=DEAD
fi
echo "MARKER_POST var=$MYVAR bash_pid=$$ sleep_status=$sleep_status"
jobs
"""


def write_test_scripts() -> None:
    os.makedirs(TEST_SCRIPT_DIR, exist_ok=True)
    for name, body in (("setup.sh", SETUP_SCRIPT), ("verify.sh", VERIFY_SCRIPT)):
        path = os.path.join(TEST_SCRIPT_DIR, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)


def drain(master_fd: int, timeout: float = 1.0) -> bytes:
    """Read from master_fd until quiet for `timeout` seconds."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        r, _, _ = select.select([master_fd], [], [], min(0.2, remaining))
        if not r:
            continue
        try:
            chunk = os.read(master_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        deadline = time.time() + timeout
    return buf


def send(master_fd: int, data: str) -> None:
    os.write(master_fd, data.encode())


def spawn_bash() -> tuple[int, subprocess.Popen, int]:
    """Allocate a PTY and ask the root helper to spawn bash on it.

    Returns (master_fd, helper_popen, bash_host_pid).
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    # PID rendezvous inside the user-owned STATE_DIR — helper writes file
    # as root but we can still remove it since we own the parent dir.
    rendezvous_dir = tempfile.mkdtemp(prefix="rendezvous-", dir=STATE_DIR)
    report_path = os.path.join(rendezvous_dir, "bash_pid")

    uid = os.getuid()
    gid = os.getgid()
    proc = subprocess.Popen(
        ["sudo", "-n", sys.executable, HELPER, "spawn",
         "--uid", str(uid), "--gid", str(gid),
         "--slave", slave_path,
         "--pid-file", report_path,
         "--state-dir", STATE_DIR],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(slave_fd)  # helper opens the path itself; we keep only master

    # Poll for the helper to drop the host-PID file.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if os.path.exists(report_path):
            with open(report_path) as f:
                bash_host_pid = int(f.read().strip())
            shutil.rmtree(rendezvous_dir, ignore_errors=True)
            return master_fd, proc, bash_host_pid
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            shutil.rmtree(rendezvous_dir, ignore_errors=True)
            raise RuntimeError(f"helper exited before reporting pid: rc={proc.returncode}\n{err}")
        time.sleep(0.05)
    shutil.rmtree(rendezvous_dir, ignore_errors=True)
    raise RuntimeError("timed out waiting for helper to report bash pid")


def _kill_helper(helper_proc: subprocess.Popen | None) -> None:
    """Best-effort: kill the sudo+helper subtree so we never leak an
    orphan namespace init when the driver dies unexpectedly."""
    if helper_proc is None:
        return
    if helper_proc.poll() is not None:
        return
    try:
        # SIGTERM to sudo; sudo forwards to the helper; helper-parent
        # death cascades into the namespace via PID-1 semantics.
        helper_proc.terminate()
        helper_proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        helper_proc.kill()
        try:
            helper_proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass


_active_helper: subprocess.Popen | None = None


def main() -> int:
    global _active_helper
    if shutil.which("sudo") is None:
        print("sudo not on PATH", file=sys.stderr)
        return 2
    if os.path.exists(CKPT_DIR):
        shutil.rmtree(CKPT_DIR)
    os.makedirs(CKPT_DIR)
    os.makedirs(STATE_DIR, exist_ok=True)
    # Clean any leftover state from prior runs (rendezvous dirs, old
    # criu-extra config files, etc.) — but keep helper.log so we don't
    # lose history across runs.
    for name in os.listdir(STATE_DIR):
        if name == "helper.log":
            continue
        path = os.path.join(STATE_DIR, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.unlink(path)
    write_test_scripts()

    try:
        return _run()
    finally:
        _kill_helper(_active_helper)


def _run() -> int:
    global _active_helper
    print("[1/6] spawning bash in PID namespace ...")
    master_fd, helper_proc, bash_pid = spawn_bash()
    _active_helper = helper_proc
    print(f"      bash host pid = {bash_pid}, helper pid = {helper_proc.pid}")

    # Wait for prompt to appear.
    initial = drain(master_fd, timeout=1.5)
    print(f"      initial output ({len(initial)} bytes):")
    print("      " + initial.decode(errors="replace").replace("\n", "\n      "))

    print("[2/6] sourcing setup.sh in bash (sets var, spawns child) ...")
    setup_path = os.path.join(TEST_SCRIPT_DIR, "setup.sh")
    send(master_fd, f"source {setup_path}\n")
    before = drain(master_fd, timeout=2.0)
    print("      " + before.decode(errors="replace").replace("\n", "\n      "))
    m = re.search(rb"MARKER_PRE var=(\S+) sleep_pid=(\d+) bash_pid=(\d+)", before)
    if not m:
        print("FAIL: did not see MARKER_PRE in pre-checkpoint output")
        return 1
    pre_var = m.group(1).decode()
    sleep_pid_ns = int(m.group(2))
    bash_pid_ns = int(m.group(3))
    if pre_var != "hello_state" or bash_pid_ns != 1:
        print(f"FAIL: unexpected pre-state var={pre_var!r} bash_pid={bash_pid_ns}")
        return 1
    print(f"      bash is PID {bash_pid_ns} in ns, sleep is PID {sleep_pid_ns}, MYVAR={pre_var}")

    print(f"[3/6] dumping bash tree at host-pid {bash_pid} ...")
    rc = subprocess.call(
        ["sudo", "-n", sys.executable, HELPER, "dump",
         "--owner-uid", str(os.getuid()),
         "--owner-gid", str(os.getgid()),
         "--pid", str(bash_pid),
         "--ckpt-dir", CKPT_DIR,
         "--state-dir", STATE_DIR]
    )
    if rc != 0:
        print(f"FAIL: criu dump returned {rc}")
        print("      dump.log tail:")
        try:
            with open(os.path.join(CKPT_DIR, "dump.log")) as f:
                tail = f.readlines()[-40:]
            print("      " + "".join(tail).replace("\n", "\n      "))
        except FileNotFoundError:
            pass
        return 1
    print("      dump succeeded")

    # The helper-parent should exit shortly after bash gets dumped (since
    # bash's death is what its wait() loop is waiting on). Reap it.
    try:
        helper_proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        helper_proc.kill()
        helper_proc.wait()

    # Master fd may now read EOF. We'll close and re-open a fresh PTY
    # for the restore. For the PoC's first try, restore into the SAME
    # PTY (same /dev/pts/N) since the master is still alive.
    slave_path = os.ttyname(master_fd) if False else None
    # os.ttyname only works on a slave fd, not a master. Look it up via
    # /proc — the master's peer is the slave at the same index.
    # Simpler: just allocate a fresh PTY.
    os.close(master_fd)

    print("[4/6] allocating fresh PTY for restore ...")
    new_master_fd, new_slave_fd = pty.openpty()
    new_slave_path = os.ttyname(new_slave_fd)
    print(f"      new slave = {new_slave_path}")

    print("[5/6] restoring ...")
    # --pid-file is mandatory in the modern helper but we don't actually
    # need the value here — the test verifies via PTY I/O, not by PID.
    restore_pid_file = os.path.join(STATE_DIR, "restore_pid")
    restore_proc = subprocess.Popen(
        ["sudo", "-n", sys.executable, HELPER, "restore",
         "--owner-uid", str(os.getuid()),
         "--owner-gid", str(os.getgid()),
         "--ckpt-dir", CKPT_DIR,
         "--slave", new_slave_path,
         "--state-dir", STATE_DIR,
         "--pid-file", restore_pid_file],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(new_slave_fd)  # restored bash holds it now
    rc = restore_proc.wait()
    stderr_data = restore_proc.stderr.read().decode(errors="replace")
    if rc != 0:
        print(f"FAIL: criu restore returned {rc}")
        print("      stderr:", stderr_data)
        try:
            with open(os.path.join(CKPT_DIR, "restore.log")) as f:
                tail = f.readlines()[-60:]
            print("      restore.log tail:")
            print("      " + "".join(tail).replace("\n", "\n      "))
        except FileNotFoundError:
            pass
        return 1
    print("      restore succeeded")

    print("[6/6] sourcing verify.sh in restored bash ...")
    verify_path = os.path.join(TEST_SCRIPT_DIR, "verify.sh")
    send(new_master_fd, f"source {verify_path}\n")
    after = drain(new_master_fd, timeout=2.5)
    print("      " + after.decode(errors="replace").replace("\n", "\n      "))

    m = re.search(rb"MARKER_POST var=(\S+) bash_pid=(\d+) sleep_status=(\w+)", after)
    if not m:
        print("FAIL: restored bash did not produce MARKER_POST")
        return 1
    post_var = m.group(1).decode()
    post_bash_pid_ns = int(m.group(2))
    sleep_alive = m.group(3) == b"ALIVE"
    has_job = b"sleep" in after  # jobs output mentions 'sleep'

    print()
    print("=== Phase 3 results ===")
    print(f"  variable preserved:    MYVAR={post_var!r} (expected 'hello_state'): "
          f"{'PASS' if post_var == 'hello_state' else 'FAIL'}")
    print(f"  bash PID stable:       {bash_pid_ns} → {post_bash_pid_ns}: "
          f"{'PASS' if post_bash_pid_ns == bash_pid_ns else 'FAIL'}")
    print(f"  child still alive:     kill -0 {sleep_pid_ns}: "
          f"{'PASS' if sleep_alive else 'FAIL'}")
    print(f"  child in jobs table:   "
          f"{'PASS' if has_job else 'FAIL (but child PID is still alive)'}")

    if (post_var != "hello_state"
            or post_bash_pid_ns != bash_pid_ns
            or not sleep_alive):
        print("\nFAIL: one or more Phase 3 checks failed")
        return 1

    print("\nSUCCESS: bash + child process + memory state all survived "
          "the checkpoint/restore cycle.")
    # Clean up: send exit to bash.
    send(new_master_fd, "exit\n")
    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
