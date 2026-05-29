# Per-tab process checkpoint / restore via CRIU

This document describes the design of an **optional** feature that lets
Terminator suspend the processes running inside its tabs to disk,
survive a reboot, and restore them on next launch. The state is the
real Linux process state (memory, open files, TTY connection),
restored via [CRIU][criu] — not a "logical" re-spawn of the command line.

[criu]: https://criu.org/

## Important: this feature is opt-in

Terminator continues to function as a normal terminal emulator without
CRIU installed. Nothing in this branch:

- Adds CRIU as a hard dependency in `setup.py` `install_requires`.
- Touches `/etc/sudoers.d/` or creates system groups during
  `python3 setup.py install`.
- Imports `pycriu` from any module Terminator loads at startup.

When CRIU is absent at runtime, the checkpoint/restore menu entries are
disabled with a tooltip that points the user at the install steps below.
The rest of Terminator behaves exactly as before.

## Goals and non-goals

**Goals.** Make `vim` sessions, REPLs, paused shells, and other
self-contained TTY-attached processes survive a reboot in the same tab
they were in. Make the feature **optional, per-tab, and explicit** — no
surprise behavior, no implicit costs.

**Non-goals.** Restore processes whose state lives outside the local
machine (active SSH connections, sockets to external services). CRIU
cannot reconstruct those, and trying makes the failure modes murky.
Such tabs simply aren't checkpoint-eligible; the UI says so.

## Enabling per profile

Per-profile, via **Preferences → Profile → Command → "Enable
checkpoint/restore (CRIU) for tabs in this profile"** (`checkpoint_enabled`
in `~/.config/terminator/config`). Off by default on every profile.
New tabs spawned with a profile that has this flag set are wrapped by
the helper (PID + mount namespace + privilege drop) so they can later
be checkpointed; tabs with non-checkpoint profiles behave like
unmodified terminator tabs.

A per-tab toggle is also exposed in the right-click menu
("Checkpoint c**a**pable") so the user can suppress checkpointing for
a single tab without flipping the profile-wide default.

## Triggers — when checkpoints are taken or consumed

| Event | Action | Tab fate |
|---|---|---|
| Right-click → "C**h**eckpoint this tab" | Dump just this tab; replace any older dump | keeps running |
| **Ctrl-D / explicit close of one tab** | Delete that tab's checkpoint dir | dies |
| **Close the whole window** | Dump every CRIU tab, save session, mark preserve | dies |
| **OS shutdown** (logind `PrepareForShutdown`) | Dump every CRIU tab, save session, mark preserve | dies |
| **Laptop sleep** (logind `PrepareForSleep`) | Dump every CRIU tab as a power-loss safety net | keeps running |
| **Screen lock / blank** (`org.freedesktop.ScreenSaver.ActiveChanged`) | Same as laptop sleep | keeps running |
| **`Ctrl-C` on terminator's launching shell** | Same as window close | dies |
| **External DBus call** (`criu_checkpoint_all` on net.tenshu.Terminator2) | Same as laptop sleep | keeps running |
| **Restore on next launch** (session.json present, no `--layout` given) | Spawn each tab via `criu restore`; consume each dump dir | recreated |

Auto-save lifecycle hooks (close, shutdown, sleep, screensaver, etc.)
are no-ops when no tab in the window is currently CRIU-active —
nothing on disk is touched and no session.json is written. Users who
never opt into checkpointing see no behavioral change.

A *delay* inhibit lock is held on `shutdown:sleep` while terminator
is running so logind waits for the dump before tearing the process
down. The lock auto-releases on process exit.

## Architecture in one paragraph

Each "checkpointable" tab is spawned inside a fresh PID + mount
namespace by a small root helper. The shell or program inside the tab
runs as the user's normal UID — the helper drops privileges before
`exec`. On checkpoint, CRIU dumps the process tree to disk; on restore,
CRIU recreates the tree and plumbs a freshly-allocated VTE PTY in place
of the originally-checkpointed one via `--inherit-fd`. Terminator
itself stays unprivileged — it shells out to the helper for the four
operations that need root.

## Components

| Path | Role |
|---|---|
| `libexec/terminator-criu-helper` | Privileged helper. Installed by `setup.py` to `<prefix>/bin/terminator-criu-helper`. |
| `libexec/terminator-criu-setup` | One-shot admin script that creates the `terminator-criu` group, installs the validated sudoers fragment, and adds the user to the group. Run by hand: `sudo terminator-criu-setup`. |
| `data/criu/terminator-criu.sudoers` | Sudoers template. Shipped to `<prefix>/share/terminator/criu/` by `setup.py`; **not** auto-deployed to `/etc/sudoers.d/`. |
| `terminatorlib/criu/` | Unprivileged Python imported by Terminator. Detects whether CRIU + helper are available, hides UI gracefully if not. |
| `integration_tests/criu/` | End-to-end tests that need CRIU, sudo, and `CAP_SYS_ADMIN`. Excluded from `pytest` by the root `conftest.py`. |

## Install

**`setup.py` auto-detects CRIU.** Running `python3 setup.py install` on
a system that doesn't have `criu` and `pycriu` installs Terminator
without any CRIU files. setup.py prints a one-line note at the top of
its output telling you which path it took. If you install CRIU later
and want the integration, re-run `python3 setup.py install` — the
helper + sudoers template + `terminatorlib.criu` package will be
included this time.

End-user opt-in flow:

```
# 1. Install CRIU first (terminator-criu-setup prints distro guidance
#    if you do this in the wrong order). On Ubuntu noble (24.04):
sudo add-apt-repository ppa:criu/ppa
sudo apt-get update
sudo apt-get install criu python3-pycriu

# 2. Install / re-install terminator. setup.py detects CRIU and
#    includes the checkpoint/restore files this time.
sudo python3 setup.py install

# 3. One-shot system configuration: group, sudoers, user membership.
sudo terminator-criu-setup

# 4. Verify end-to-end.
sudo terminator-criu-helper check
```

When the user next launches Terminator, the checkpoint/restore menu
items will be enabled.

## Security model

The privileged surface is exactly one binary at one fixed path. The
sudoers rule grants `NOPASSWD` access only to that binary, and only to
its four named subcommands (`check`, `spawn`, `dump`, `restore`). The
helper:

- Drops privileges (`setgid`+`setuid`) before `exec`'ing any user
  program. The user's tab shell is never privileged.
- Chowns its produced artifacts (CRIU dumps, logs) back to the caller
  before exiting so the unprivileged caller can read and clean up.
- Uses `prctl(PR_SET_PDEATHSIG, SIGKILL)` so a crashed Terminator never
  leaves an orphan namespace-init process alive.

The unprivileged Python side never imports `pycriu` and never touches
root. Each operation is a fresh `sudo` invocation; the helper exits
between operations.

## Mount-namespace discipline

The helper marks `/` as `MS_REC|MS_SLAVE` immediately after
`unshare(CLONE_NEWNS)` so mounts inside the namespace cannot propagate
back to the host. This is non-negotiable: skipping it once corrupted
the host `/proc` mount table during PoC development.

The helper also prunes inherited host mounts (`/sys`, `/run`, `/snap`,
etc.) from its own namespace before CRIU runs — CRIU's mount engine
struggles with the modern systemd mount tree (snap loops in particular
cause "external slavery" errors). The prune is done via
`umount2(MNT_DETACH)` via `ctypes` so it works regardless of which
`umount(8)` is on `PATH` and never touches the host.

## Scrollback (visible terminal history) is preserved too

CRIU restores process state (memory, file descriptors, etc.) but it
has no notion of the terminal-emulator buffer that VTE renders. To
make a restored tab feel continuous, we capture the VTE buffer
alongside the CRIU dump and replay it on restore:

- **Save**: just before each `criu dump`, write
  `<ckpt_dir>/scrollback.vte` using
  `Vte.Terminal.write_contents_sync(stream, WriteFlags.DEFAULT)`.
  That format is VTE's own contents stream — colors, cursor positions,
  attributes — designed to be fed back into another VTE instance.
- **Restore**: after `vte.set_pty(...)` but before
  `criu_client.restore()` (so the buffer is populated *before* the
  resumed process writes anything new), read `scrollback.vte` and
  `vte.feed(bytes)`. The historical view appears in the new tab, then
  fresh output from the restored process appends after.

A per-profile toggle (**Preferences → Profile → Command → "Restore
scrollback history on checkpoint restore"**,
`checkpoint_restore_scrollback` in `~/.config/terminator/config`)
controls just the *restore* side — defaults to **on**. Save always
runs at dump time, so the scrollback file lives on disk regardless;
turning the flag off only suppresses the replay so the restored tab
starts with a clean screen on top of the underlying process state.

## XDG paths used at runtime

| Purpose | Location |
|---|---|
| CRIU dump images per tab (persistent across reboots) | `$XDG_DATA_HOME/terminator-criu/checkpoints/<tab-uuid>/` |
| Saved VTE buffer for each checkpoint | `$XDG_DATA_HOME/terminator-criu/checkpoints/<tab-uuid>/scrollback.vte` |
| Auto-saved session (hidden, one-shot, consumed on next launch) | `$XDG_DATA_HOME/terminator-criu/session.json` |
| Helper diagnostic log, transient state | `$XDG_STATE_HOME/terminator-criu/helper.log` |

`session.json` is written by the auto-save hooks and consumed (deleted)
at next startup after its layout has been handed to `create_layout`.
On consumption, any checkpoint dir whose UUID is not referenced by
the just-loaded session is treated as orphan and removed.

`$XDG_RUNTIME_DIR` would be the textbook choice for transient state but
is unsuitable here: the helper prunes `/run` inside its mount namespace
before invoking CRIU, which makes `/run/user/$UID/...` unreachable to
the CRIU process. `$XDG_STATE_HOME` (under `/home`) survives the prune
and matches the XDG semantics for "data that persists between runs but
isn't user-portable" (debug logs).

## Dependencies summary

Required only if the user wants checkpoint/restore enabled:

- Linux kernel with `CONFIG_CHECKPOINT_RESTORE=y` (any modern distro).
- `criu` ≥ 4.0 installed system-wide. On Ubuntu 24.04 it's not in the
  default repos; use `ppa:criu/ppa`.
- `python3-pycriu` (Python bindings, shipped alongside the `criu`
  package).
- A `terminator-criu` group, created by `terminator-criu-setup`.

None of the above are needed for a standard Terminator install.

## Note for distro packagers

The upstream Terminator repository does not carry packaging (see
`INSTALL.md` — Debian packaging lives at salsa.debian.org). This branch
adds no `debian/` scaffolding either.

What downstream packagers may want to do:

- Stage the new files via `dh_install` (or distro equivalent): the
  helper at `/usr/bin/terminator-criu-helper`, the setup script at
  `/usr/bin/terminator-criu-setup`, the sudoers template at
  `/usr/share/terminator/criu/terminator-criu.sudoers`.
- Decide whether to install `/etc/sudoers.d/terminator-criu`
  automatically (e.g. via debconf, with a default of "off") or to
  require the user to run `terminator-criu-setup` by hand. Both work;
  the `terminator-criu-setup` script is idempotent and non-interactive
  so either is safe.
- `criu` and `python3-pycriu` should be `Recommends`, not `Depends` —
  the integration is strictly opt-in and Terminator works without
  them. `setup.py`'s auto-detection skips the CRIU files entirely if
  they're not present at build time.

## Notes on intentional design choices

**The hidden session captures the *whole* arrangement, not just CRIU
tabs.** When any tab in a window has a checkpoint dir on disk, the
session-save hooks (window close, sleep, screen-lock, etc.) write the
complete layout — including non-CRIU tabs — into `session.json`. On
next launch, CRIU tabs are restored from their dumps, while non-CRIU
tabs come back as freshly-spawned shells in the same spatial position
(same window, same tab index, same split pane). This preserves the
user's window layout intuition: "all my tabs come back, the ones I
wanted preserved actually have my work in them." A future option
could let users opt out of layout-only restoration if it's surprising.

**Auto-checkpoint events leave tabs running for "safety net" triggers
(sleep, screen-lock, DBus kick) but let them die for "exit" triggers
(window close, OS shutdown, Ctrl-C).** This matches the user's mental
model: closing the window means "I'm done for now — bring this back
later"; locking the screen means "I'll be back soon — keep working."
A clean exit (Ctrl-D inside a tab) bypasses preservation entirely.

**Background auto-checkpoints surface their result in the right-click
menu's "Checkpoint capable" tooltip.** Since lifecycle events (sleep,
shutdown, screensaver) often fire while the user isn't at the
keyboard, the toggle's tooltip carries a "Last auto-checkpoint: N
tab(s), Xs/m/h ago, OK | M FAILED" footer so subsequent right-clicks
expose what happened. Failure detail still goes to stderr and
`$XDG_STATE_HOME/terminator-criu/helper.log`.

## Dev: running the integration tests

The end-to-end tests need CRIU + sudo + `CAP_SYS_ADMIN`, so they're
excluded from `pytest` discovery by the root `conftest.py`. Run them by
hand:

```
python3 integration_tests/criu/test_checkpoint_bash.py
python3 integration_tests/criu/test_checkpoint_python.py
```

Each spawns a process in a PID namespace, dumps it, restores it into a
fresh PTY, and asserts in-memory state survived.
