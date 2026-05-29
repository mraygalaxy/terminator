#!/usr/bin/env python3
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""test_checkpoint_python.py - end-to-end CRIU c/r test for a Python REPL

Spawns `python3 -i setup.py` inside a PID+mount namespace via the root
helper. After setup.py runs, Python drops to its interactive REPL with
the script's variables still bound in __main__. The driver then dumps
the REPL, restores it into a fresh PTY, sources verify.py via exec(),
and asserts that:
  - a simple int and string survived
  - a mutable list survived AND remains mutable after restore
  - a custom class instance retained its attribute
  - the Python process's PID inside the namespace is stable

NOT picked up by pytest (see conftest.py at the repo root). Run manually:

    python3 integration_tests/criu/test_checkpoint_python.py
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


# See poc2_driver.py for the rationale behind this split.
STATE_DIR = os.path.join(_xdg_state_home(), "terminator-criu-poc3")
CKPT_DIR = os.path.join(_xdg_data_home(), "terminator-criu-poc3", "checkpoint")
TEST_SCRIPT_DIR = os.path.join(_xdg_data_home(), "terminator-criu-poc3", "scripts")

# Runs once at REPL start, sets state, prints a single marker line for the
# driver to parse. `import os` is here (not in the driver) so the script is
# self-contained and re-runnable by hand.
SETUP_SCRIPT = '''\
import os
x = 42
y = "preserved"
counter = [0]
class Box:
    def __init__(self, n): self.n = n
b = Box(99)
print(f"MARKER_PRE x={x} y={y!r} counter={counter} box_n={b.n} pid={os.getpid()}", flush=True)
'''

# Mutates `counter` so we can prove not just "the object still exists" but
# "the same object is still bound and still works."
VERIFY_SCRIPT = '''\
counter[0] += 1
print(f"MARKER_POST x={x} y={y!r} counter={counter} box_n={b.n} pid={os.getpid()}", flush=True)
'''


def write_test_scripts() -> None:
    os.makedirs(TEST_SCRIPT_DIR, exist_ok=True)
    for name, body in (("setup.py", SETUP_SCRIPT), ("verify.py", VERIFY_SCRIPT)):
        with open(os.path.join(TEST_SCRIPT_DIR, name), "w") as f:
            f.write(body)


def drain(master_fd: int, timeout: float = 1.0) -> bytes:
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


def spawn_python() -> tuple[int, subprocess.Popen, int]:
    """Allocate a PTY and ask the helper to run python3 -i setup.py on it."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    rendezvous_dir = tempfile.mkdtemp(prefix="rendezvous-", dir=STATE_DIR)
    report_path = os.path.join(rendezvous_dir, "python_pid")

    setup_path = os.path.join(TEST_SCRIPT_DIR, "setup.py")
    proc = subprocess.Popen(
        ["sudo", "-n", sys.executable, HELPER, "spawn",
         "--uid", str(os.getuid()), "--gid", str(os.getgid()),
         "--slave", slave_path,
         "--pid-file", report_path,
         "--state-dir", STATE_DIR,
         # `--` ends argparse's flag parsing; everything after is the
         # program-to-exec argv. Without it, argparse would interpret
         # `-u`/`-i` as flags.
         "--",
         # -u: unbuffered stdout. -i: stay interactive after the script.
         "/usr/bin/python3", "-u", "-i", setup_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(slave_fd)

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if os.path.exists(report_path):
            with open(report_path) as f:
                python_host_pid = int(f.read().strip())
            shutil.rmtree(rendezvous_dir, ignore_errors=True)
            return master_fd, proc, python_host_pid
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            shutil.rmtree(rendezvous_dir, ignore_errors=True)
            raise RuntimeError(f"helper exited before reporting pid: rc={proc.returncode}\n{err}")
        time.sleep(0.05)
    shutil.rmtree(rendezvous_dir, ignore_errors=True)
    raise RuntimeError("timed out waiting for helper to report python pid")


def _kill_helper(helper_proc: subprocess.Popen | None) -> None:
    if helper_proc is None or helper_proc.poll() is not None:
        return
    try:
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
    print("[1/6] spawning python3 -i setup.py in PID namespace ...")
    master_fd, helper_proc, python_pid = spawn_python()
    _active_helper = helper_proc
    print(f"      python host pid = {python_pid}, helper pid = {helper_proc.pid}")

    # Drain initial output — should include MARKER_PRE and then the >>> prompt.
    initial = drain(master_fd, timeout=2.0)
    print("      " + initial.decode(errors="replace").replace("\n", "\n      "))

    m = re.search(rb"MARKER_PRE x=(\d+) y=(\S+) counter=\[(\d+)\] box_n=(\d+) pid=(\d+)", initial)
    if not m:
        print("FAIL: did not see MARKER_PRE in REPL startup output")
        return 1
    pre_x = int(m.group(1))
    pre_y = m.group(2).decode()
    pre_counter = int(m.group(3))
    pre_box_n = int(m.group(4))
    pre_pid_ns = int(m.group(5))
    print(f"      pre-state: x={pre_x}, y={pre_y}, counter[0]={pre_counter}, "
          f"box.n={pre_box_n}, python PID in ns={pre_pid_ns}")

    print(f"[2/6] dumping python tree at host-pid {python_pid} ...")
    rc = subprocess.call(
        ["sudo", "-n", sys.executable, HELPER, "dump",
         "--owner-uid", str(os.getuid()),
         "--owner-gid", str(os.getgid()),
         "--pid", str(python_pid),
         "--ckpt-dir", CKPT_DIR,
         "--state-dir", STATE_DIR]
    )
    if rc != 0:
        print(f"FAIL: criu dump returned {rc}")
        try:
            with open(os.path.join(CKPT_DIR, "dump.log")) as f:
                print("      dump.log tail:")
                print("      " + "".join(f.readlines()[-40:]).replace("\n", "\n      "))
        except (FileNotFoundError, PermissionError):
            pass
        return 1
    print("      dump succeeded")

    try:
        helper_proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        helper_proc.kill()
        helper_proc.wait()

    os.close(master_fd)

    print("[3/6] allocating fresh PTY for restore ...")
    new_master_fd, new_slave_fd = pty.openpty()
    new_slave_path = os.ttyname(new_slave_fd)
    print(f"      new slave = {new_slave_path}")

    print("[4/6] restoring ...")
    # --pid-file is required by the modern helper; the test ignores
    # the value and verifies behavior through PTY I/O.
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
    os.close(new_slave_fd)
    rc = restore_proc.wait()
    stderr_data = restore_proc.stderr.read().decode(errors="replace") if restore_proc.stderr else ""
    if rc != 0:
        print(f"FAIL: criu restore returned {rc}")
        print("      stderr:", stderr_data)
        try:
            with open(os.path.join(CKPT_DIR, "restore.log")) as f:
                print("      restore.log tail:")
                print("      " + "".join(f.readlines()[-60:]).replace("\n", "\n      "))
        except (FileNotFoundError, PermissionError):
            pass
        return 1
    print("      restore succeeded")

    # Wait briefly for the restored REPL to settle and emit its prompt.
    _ = drain(new_master_fd, timeout=1.0)

    print("[5/6] running verify.py via exec() in restored REPL ...")
    verify_path = os.path.join(TEST_SCRIPT_DIR, "verify.py")
    # exec() at REPL top-level updates globals(), so verify.py sees x, y,
    # counter, b just fine.
    send(new_master_fd, f"exec(open({verify_path!r}).read())\n")
    after = drain(new_master_fd, timeout=2.5)
    print("      " + after.decode(errors="replace").replace("\n", "\n      "))

    m = re.search(rb"MARKER_POST x=(\d+) y=(\S+) counter=\[(\d+)\] box_n=(\d+) pid=(\d+)", after)
    if not m:
        print("FAIL: did not see MARKER_POST in restored REPL output")
        return 1
    post_x = int(m.group(1))
    post_y = m.group(2).decode()
    post_counter = int(m.group(3))
    post_box_n = int(m.group(4))
    post_pid_ns = int(m.group(5))

    print()
    print("[6/6] checking results ...")
    print("=== Phase 3 (Python REPL) results ===")
    checks = [
        ("x (int) preserved",
         post_x == 42, f"expected 42, got {post_x}"),
        ("y (str) preserved",
         post_y == "'preserved'", f"expected 'preserved', got {post_y}"),
        ("counter (mutable list) survived AND was mutable post-restore",
         post_counter == pre_counter + 1,
         f"expected pre+1 ({pre_counter}+1), got {post_counter}"),
        ("Box instance attribute preserved",
         post_box_n == 99, f"expected 99, got {post_box_n}"),
        ("Python PID inside ns stable",
         post_pid_ns == pre_pid_ns,
         f"expected {pre_pid_ns}, got {post_pid_ns}"),
    ]
    all_pass = True
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}" + (f" — {detail}" if not ok else ""))

    if not all_pass:
        return 1

    # Be polite — quit Python so the helper-parent reaps cleanly.
    send(new_master_fd, "exit()\n")
    time.sleep(0.2)

    print("\nSUCCESS: Python REPL with rich in-memory state survived c/r.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
