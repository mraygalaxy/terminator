# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""terminatorlib.criu.client - unprivileged terminator-side glue for CRIU.

This module is what the rest of terminator imports. It MUST tolerate
CRIU being absent: a successful import never depends on the helper
being installed or criu being available. Callers ask
`CriuClient().is_available()` before exposing any UI affordance.
"""

import enum
import os
import shutil
import subprocess
import tempfile
import time


# Possible install locations for the privileged helper, in priority order.
# Matches what setup.py's `scripts=` produces under common --prefix values.
_HELPER_CANDIDATES = (
    "/usr/local/bin/terminator-criu-helper",
    "/usr/bin/terminator-criu-helper",
)


def _find_helper():
    """Return the absolute path of the helper if installed, else None.

    Dev mode: if terminator is being run from a source checkout (no
    `setup.py install` yet), look for the helper in the sibling
    libexec/ directory of the repo so the feature is testable
    without installing. Installed paths win when both exist.
    """
    # 1. Installed paths (production).
    for p in _HELPER_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # 2. PATH lookup (in case installed somewhere unusual).
    found = shutil.which("terminator-criu-helper")
    if found and os.access(found, os.X_OK):
        return found
    # 3. Repo source layout (development).
    here = os.path.dirname(os.path.abspath(__file__))
    # __file__ is .../terminatorlib/criu/client.py → repo is two up.
    repo_root = os.path.dirname(os.path.dirname(here))
    dev_helper = os.path.join(repo_root, "libexec", "terminator-criu-helper")
    if os.path.isfile(dev_helper) and os.access(dev_helper, os.X_OK):
        return dev_helper
    return None


class Status(enum.Enum):
    """Coarse availability buckets, distinct enough to drive UI tooltips."""

    READY = "ready"
    HELPER_MISSING = "helper_missing"
    SUDO_NEEDS_PASSWORD = "sudo_needs_password"
    CRIU_CHECK_FAILED = "criu_check_failed"
    UNKNOWN_ERROR = "unknown_error"


# Human-readable detail text the UI can show. Each message is the
# CAUSE only — callers prepend their own context (e.g. "Checkpoint
# unavailable: ...", or "Falling back: ..."). Keeps the strings
# composable instead of having multiple "unavailable" preambles
# concatenated together.
_STATUS_MESSAGES = {
    Status.READY:
        "ready",
    Status.HELPER_MISSING:
        "the terminator-criu-helper binary is not installed. "
        "Install CRIU + python3-pycriu and re-run "
        "`python3 setup.py install`, then `sudo terminator-criu-setup`. "
        "See CRIU.md.",
    Status.SUDO_NEEDS_PASSWORD:
        "sudo wants a password for the helper. Run "
        "`sudo terminator-criu-setup` and ensure your user is in the "
        "`terminator-criu` group (log out + back in to apply). "
        "See CRIU.md.",
    Status.CRIU_CHECK_FAILED:
        "`criu check` failed on this kernel. Verify "
        "CONFIG_CHECKPOINT_RESTORE is enabled and you have CRIU >= 4.0. "
        "Run `sudo terminator-criu-helper check` for details.",
    Status.UNKNOWN_ERROR:
        "an unexpected error occurred probing the helper. Run "
        "`sudo terminator-criu-helper check` manually for details.",
}


class CriuSpawnError(Exception):
    """Raised when the helper can't be invoked or fails before reporting
    the spawned program's PID."""


class CriuOperationError(Exception):
    """Raised when a dump or restore via the helper fails. The message
    includes the helper's stderr tail, which itself includes the
    relevant CRIU log lines."""


class SpawnResult(object):
    """What CriuClient.spawn() hands back to the caller.

    Attributes:
        host_pid: the host-namespace PID of the spawned program (i.e.
            what the caller would use for `kill()` etc.). Inside the
            program's own PID namespace it's PID 1.
        helper_proc: the subprocess.Popen handle for the sudo+helper
            chain. Stays alive (as PID 1 of the new namespace) until
            the spawned program exits. Caller should keep a reference
            so it gets reaped.
        slave_path: the PTY slave path the helper attached to. Same
            string the caller passed in; returned for convenience.
    """
    def __init__(self, host_pid, helper_proc, slave_path):
        self.host_pid = host_pid
        self.helper_proc = helper_proc
        self.slave_path = slave_path


def checkpoint_dir_for_uuid(uuid_hex):
    """Return the per-tab checkpoint directory path for a UUID hex
    string. Caller is responsible for creating the parents if it wants
    to write there before invoking dump (the helper creates ckpt_dir
    itself, so this is only needed for prior enumeration / cleanup)."""
    base = os.environ.get("XDG_DATA_HOME") or \
        os.path.expanduser("~/.local/share")
    return os.path.join(base, "terminator-criu", "checkpoints", uuid_hex)


def _state_dir():
    """Return $XDG_STATE_HOME/terminator-criu (created if needed).

    Falls back to ~/.local/state/terminator-criu per the XDG spec when
    $XDG_STATE_HOME is unset. NOT under $XDG_RUNTIME_DIR because the
    helper prunes /run inside its mount namespace; see CRIU.md.
    """
    base = os.environ.get("XDG_STATE_HOME") or \
        os.path.expanduser("~/.local/state")
    return os.path.join(base, "terminator-criu")


class CriuClient(object):
    """Unprivileged wrapper around the root helper.

    Probing `terminator-criu-helper check` is the source of truth for
    "can the user actually checkpoint things right now?". The result is
    cached after the first call; pass force_recheck=True to re-probe
    (e.g. after the user runs terminator-criu-setup in a separate shell).
    """

    # Sudo emits this when -n is given but NOPASSWD isn't configured.
    # We match on a substring so localized variants still trigger.
    _SUDO_NOPASSWD_FAILURE_MARKERS = (
        "a password is required",
        "password is required",
        "no tty present",  # also possible in some sudoers configs
    )

    def __init__(self):
        self._helper = _find_helper()
        self._cached_status = None

    @property
    def helper_path(self):
        return self._helper

    def status(self, force_recheck=False):
        """Return a Status enum value for the current state of the helper."""
        if self._cached_status is not None and not force_recheck:
            return self._cached_status
        self._cached_status = self._probe()
        return self._cached_status

    def is_available(self, force_recheck=False):
        return self.status(force_recheck) == Status.READY

    def status_message(self, force_recheck=False):
        return _STATUS_MESSAGES[self.status(force_recheck)]

    def _probe(self):
        # Re-resolve the helper path each probe — handles the case
        # where the binary was installed/moved/uninstalled since the
        # client was constructed (without this, force_recheck=True
        # can't recover from a stale cached path).
        self._helper = _find_helper()
        if self._helper is None or not os.access(self._helper, os.X_OK):
            return Status.HELPER_MISSING
        try:
            result = subprocess.run(
                ["sudo", "-n", self._helper, "check"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return Status.UNKNOWN_ERROR
        if result.returncode == 0:
            return Status.READY
        stderr_lower = (result.stderr or "").lower()
        if any(m in stderr_lower for m in self._SUDO_NOPASSWD_FAILURE_MARKERS):
            return Status.SUDO_NEEDS_PASSWORD
        # If sudo says "command not found" the cached helper is stale —
        # report as missing rather than misdiagnosing as a kernel issue.
        if "command not found" in stderr_lower or "no such file" in stderr_lower:
            return Status.HELPER_MISSING
        # Helper ran (sudo didn't reject) but `criu check` returned nonzero.
        return Status.CRIU_CHECK_FAILED

    # ----- spawn ----------------------------------------------------------

    def spawn(self, slave_path, program_argv, env=None,
              owner_uid=None, owner_gid=None, timeout=10.0):
        """Ask the privileged helper to fork+exec `program_argv` inside
        a fresh PID + mount namespace, attached to `slave_path` (the
        slave half of a PTY whose master is held by VTE).

        `env` is an optional dict of KEY=VALUE that the helper writes
        to a temp file and uses verbatim as the exec env (with HOME /
        USER / SHELL etc. backfilled if missing).

        Returns a SpawnResult on success. Raises CriuSpawnError if the
        helper isn't installed, exits before reporting a PID, or times
        out.

        Caller is responsible for:
          - Holding the PTY master fd (e.g. via Vte.Pty.set_pty()).
          - Reaping the returned helper_proc when the tab closes.
        """
        if self._helper is None:
            raise CriuSpawnError("terminator-criu-helper is not installed")
        if owner_uid is None:
            owner_uid = os.getuid()
        if owner_gid is None:
            owner_gid = os.getgid()

        state_dir = _state_dir()
        os.makedirs(state_dir, exist_ok=True)

        # Per-spawn rendezvous dir holds the env file + PID file. Created
        # by us (so it's user-owned and we can rm it), used by root via
        # the helper. After we see the PID file we can safely tear down.
        rendezvous_dir = tempfile.mkdtemp(prefix="rendezvous-", dir=state_dir)
        report_path = os.path.join(rendezvous_dir, "pid")
        env_file_path = None
        if env:
            env_file_path = os.path.join(rendezvous_dir, "env")
            with open(env_file_path, "w") as f:
                for k, v in env.items():
                    if "\n" in str(v) or "\n" in str(k):
                        # Helper's parser is line-based; refuse multi-line
                        # values rather than truncating silently.
                        continue
                    f.write("%s=%s\n" % (k, v))

        cmd = ["sudo", "-n", self._helper, "spawn",
               "--uid", str(owner_uid),
               "--gid", str(owner_gid),
               "--slave", slave_path,
               "--pid-file", report_path,
               "--state-dir", state_dir]
        if env_file_path:
            cmd.extend(["--env-file", env_file_path])
        cmd.append("--")
        cmd.extend(program_argv)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if os.path.exists(report_path):
                    with open(report_path) as f:
                        host_pid = int(f.read().strip())
                    return SpawnResult(host_pid=host_pid,
                                       helper_proc=proc,
                                       slave_path=slave_path)
                if proc.poll() is not None:
                    err_text = ""
                    if proc.stderr is not None:
                        try:
                            err_text = proc.stderr.read().decode(
                                errors="replace")
                        except Exception:
                            pass
                    raise CriuSpawnError(
                        "helper exited rc=%d before reporting pid: %s"
                        % (proc.returncode, err_text))
                time.sleep(0.05)
            # Timed out — kill the helper so we don't leak.
            try:
                proc.terminate()
            except Exception:
                pass
            raise CriuSpawnError(
                "timed out after %.1fs waiting for helper to report pid"
                % timeout)
        finally:
            # Always remove the rendezvous dir. The env file (if any) has
            # already been read by the helper before it writes the PID,
            # so we're not racing.
            shutil.rmtree(rendezvous_dir, ignore_errors=True)

    # ----- dump -----------------------------------------------------------

    def dump(self, host_pid, ckpt_dir, leave_running=True,
             owner_uid=None, owner_gid=None, timeout=60.0):
        """Checkpoint the process tree rooted at `host_pid` to `ckpt_dir`.

        `leave_running=True` (the default for UI-initiated checkpoints)
        keeps the dumped process running after the snapshot so the
        user's tab continues to work — the dump just becomes a saved
        restore-point. Pass False when you want CRIU's normal
        "snapshot-then-die" behavior (e.g. an explicit "checkpoint and
        quit" action).

        Returns None on success. Raises CriuOperationError on failure,
        with the helper's stderr tail (which includes the CRIU log
        tail in turn) embedded in the exception message.
        """
        if self._helper is None:
            raise CriuOperationError(
                "terminator-criu-helper is not installed")
        if owner_uid is None:
            owner_uid = os.getuid()
        if owner_gid is None:
            owner_gid = os.getgid()

        state_dir = _state_dir()
        os.makedirs(state_dir, exist_ok=True)
        os.makedirs(ckpt_dir, exist_ok=True)

        cmd = ["sudo", "-n", self._helper, "dump",
               "--owner-uid", str(owner_uid),
               "--owner-gid", str(owner_gid),
               "--pid", str(host_pid),
               "--ckpt-dir", ckpt_dir,
               "--state-dir", state_dir]
        if leave_running:
            cmd.append("--leave-running")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CriuOperationError(
                "dump timed out after %.1fs" % timeout)
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()
            tail_text = "\n".join(tail[-20:])
            raise CriuOperationError(
                "dump failed (rc=%d):\n%s" % (result.returncode, tail_text))

    # ----- restore --------------------------------------------------------

    def restore(self, ckpt_dir, slave_path,
                owner_uid=None, owner_gid=None, timeout=60.0):
        """Restore a previously-dumped process tree from `ckpt_dir`.

        Allocates VTE-friendly plumbing on the caller's side — the
        caller must have already wrapped a master PTY in Vte.Pty and
        passed the slave path here. The helper opens the slave inside
        the restored namespace via CRIU's --inherit-fd.

        Returns a SpawnResult-shaped object whose `host_pid` is the
        host-namespace PID of the restored root process (PID 1 inside
        the restored namespace). `helper_proc` is the already-exited
        Popen handle for the criu restore process (kept for symmetry
        with spawn(); no long-running supervisor here).
        """
        if self._helper is None:
            raise CriuOperationError(
                "terminator-criu-helper is not installed")
        if owner_uid is None:
            owner_uid = os.getuid()
        if owner_gid is None:
            owner_gid = os.getgid()
        if not os.path.isdir(ckpt_dir):
            raise CriuOperationError(
                "checkpoint directory does not exist: %s" % ckpt_dir)

        state_dir = _state_dir()
        os.makedirs(state_dir, exist_ok=True)

        rendezvous_dir = tempfile.mkdtemp(prefix="rendezvous-", dir=state_dir)
        pid_file = os.path.join(rendezvous_dir, "pid")

        cmd = ["sudo", "-n", self._helper, "restore",
               "--owner-uid", str(owner_uid),
               "--owner-gid", str(owner_gid),
               "--ckpt-dir", ckpt_dir,
               "--slave", slave_path,
               "--state-dir", state_dir,
               "--pid-file", pid_file]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            shutil.rmtree(rendezvous_dir, ignore_errors=True)
            raise CriuOperationError(
                "restore timed out after %.1fs" % timeout)

        try:
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip().splitlines()
                tail_text = "\n".join(tail[-20:])
                raise CriuOperationError(
                    "restore failed (rc=%d):\n%s"
                    % (proc.returncode, tail_text))
            if not os.path.exists(pid_file):
                raise CriuOperationError(
                    "restore returned rc=0 but produced no pid-file at %s"
                    % pid_file)
            with open(pid_file) as f:
                host_pid = int(f.read().strip())
            return SpawnResult(host_pid=host_pid,
                               helper_proc=None,
                               slave_path=slave_path)
        finally:
            shutil.rmtree(rendezvous_dir, ignore_errors=True)
