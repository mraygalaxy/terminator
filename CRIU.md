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
a single tab without flipping the profile-wide default. The per-tab
override **persists** across session save/load: once a user disables
checkpointing on a tab and that tab participates in a window-close
save, the disabled state is recorded in `session.json` and restored on
next launch. Profile-level changes do *not* retroactively override a
per-tab setting once it's been explicitly saved.

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
| **Right-click → Close on a single tab** | Send SIGHUP to fg pgrp, wait up to `graceful_kill_timeout_seconds`, wipe that tab's checkpoint dir | dies |
| **Restore on next launch** (session.json present, no `--layout` given) | Spawn each tab via `criu restore` (or, on `criu_restart` markers, freshly relaunch the saved argv); consume each dump dir | recreated |

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

The helper marks `/` as `MS_REC|MS_PRIVATE` immediately after
`unshare(CLONE_NEWNS)` so mounts inside the namespace cannot propagate
back to the host. This is non-negotiable: skipping it once corrupted
the host `/proc` mount table during PoC development.

The policy is **opt-out, not opt-in**. `unshare(CLONE_NEWNS)` clones
the entire host mount tree into the new namespace automatically —
every host mount is inherited by default. The helper then surgically
removes a small list of mounts that break CRIU's dump/restore engine,
and that's it. Anything not on that list stays mounted; we never
enumerate what to keep, because we can't (the kernel gave us
everything already).

So the question to ask when CRIU fails on a new system is always
"which specific mount caused this failure?" — not "what do I need to
add to a keep-list?". The answer goes into the prune list as the
narrowest possible entry (a path or a path+fstype pair, never a broad
sledgehammer prefix).

### What we umount and why

| Mount(s) | Why we have to umount it |
|---|---|
| `/sys/fs/cgroup` | cgroup2 hierarchy with systemd-managed propagation flags. CRIU's mount engine can't replay this from outside-the-namespace state. |
| `/run/snapd/ns/*.mnt` | Bind mounts of snap process mount-namespaces (nsfs files). Classic CRIU "external slavery" trigger. Only the `ns/` subtree is targeted — `/run/snapd.socket` (the snap CLI control socket, which lives directly in `/run`) stays reachable, so `snap install` continues to work. |
| `/run/user/<uid>/doc`, `/run/user/<uid>/gvfs` (only when type matches `fuse.*`) | xdg-document-portal and gvfsd-fuse mounts whose backing daemons live outside our namespace and cannot survive into it. Targeted by **(path prefix, fstype prefix)** so the user's own fuse mounts (sshfs to a project dir, encfs, archivemount, etc.) are NOT touched. |
| `/proc` | We unmount and re-mount with `mount -t proc proc /proc` so PIDs visible in `/proc/<n>/` match the `unshare(CLONE_NEWPID)` view. |

That's the entire prune set. Nothing else gets touched.

### What that means survives inside the namespace

This is not a curated allowlist — these are just consequences of
"inherit everything, umount the few entries above." Calling them out
because each one was either silently broken in an earlier prune-
heavy revision or is a recurring "why doesn't X work in my tab?"
question:

- **`/sys`** (except `cgroup`) — `nvidia-smi`, `lspci`, `lsusb`,
  `sensors`, `acpi`, anything that walks `/sys/class/{net,power_supply,
  drm,hwmon}` or `/sys/devices`, `/sys/firmware/efi/efivars`,
  `/sys/kernel/{debug,tracing,security,bpf,config}`, `/sys/fs/pstore`.
  Critical for hardware-aware CLI work.
- **`/run`** (except the surgical bits above) — DBus user
  (`/run/user/<uid>/bus`) and system (`/run/dbus/system_bus_socket`)
  buses; Wayland socket (`wayland-0`); PulseAudio/PipeWire sockets;
  gnome-keyring (`keyring/`); `systemctl --user`; **snap CLI**
  (`/run/snapd.socket`); **avahi mDNS** (`/run/avahi-daemon/socket` —
  needed by `nss-mdns` for `.local` hostname lookups); systemd-resolved
  stub-resolv.conf for hosts whose `/etc/resolv.conf` symlinks into
  `/run/systemd/resolve/`.
- **`/snap`** — all of `/snap/<name>/<rev>` squashfs bind mounts plus
  `/snap/bin/*` wrappers. Snap-installed apps (firefox, slack, code,
  thunderbird, ...) keep launching from CLI.
- **`/dev/shm`**, **`/dev/mqueue`**, **`/dev/hugepages`** — shared
  memory tmpfs (browsers, databases, multiprocessing), POSIX message
  queues, hugetlbfs. Command-line software with shared-memory IPC or
  huge-page allocations works.
- **`/boot`**, **`/boot/efi`** — kernel images, GRUB configs, EFI
  binaries. `update-grub`, `grub-install`, kernel-tweak workflows
  work as on the host.

### Prune mechanism

Pruning is done via `umount2(MNT_DETACH)` invoked through `ctypes`
against `libc` directly, not by forking `umount(8)` — both for speed
and to avoid subprocess-related flakiness when we've already started
disturbing the mount tree. Order is deepest-first so child mounts go
before parents. `MS_REC|MS_PRIVATE` on `/` ensures none of this
propagates back to the host.

### History — why this isn't more aggressive

An earlier version pruned the entire `/sys`, `/run`, `/snap`,
`/dev/shm`, `/dev/mqueue`, `/dev/hugepages`, `/boot` subtrees. That
worked for CRIU but silently broke a long list of user-facing things
(mDNS, DBus, audio, snap CLI, nvidia-smi, browsers, GRUB tooling).
The current minimum set was reached by enumerating each user-visible
breakage, identifying which specific inherited mount actually caused
it, and proving the corresponding sub-mount was safe to keep. The
"narrow surgical removal" pattern should be how this list grows in
the future too.

## Scrollback (visible terminal history) is preserved too

CRIU restores process state (memory, file descriptors, etc.) but it
has no notion of the terminal-emulator buffer that VTE renders. To
make a restored tab feel continuous, we capture the VTE buffer
alongside the CRIU dump and replay it on restore:

- **Save**: just before each `criu dump`, write
  `<ckpt_dir>/scrollback.txt` using
  `Vte.Terminal.write_contents_sync(stream, WriteFlags.DEFAULT)`.
  That format is VTE's own contents stream — colors, cursor positions,
  attributes — designed to be fed back into another VTE instance.
- **Restore**: after `vte.set_pty(...)` but before
  `criu_client.restore()` (so the buffer is populated *before* the
  resumed process writes anything new), read `scrollback.txt` and
  `vte.feed(bytes)`. The historical view appears in the new tab, then
  fresh output from the restored process appends after. Bytes are
  CRLF-normalized first because `write_contents_sync` emits LF-only
  line terminators, which VTE would otherwise interpret as "move down
  one row, same column" — producing a diagonal staircase.

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
| Saved VTE buffer for each checkpoint | `$XDG_DATA_HOME/terminator-criu/checkpoints/<tab-uuid>/scrollback.txt` |
| Marker that a dump completed cleanly (presence = restorable) | `$XDG_DATA_HOME/terminator-criu/checkpoints/<tab-uuid>/tty_meta.json` |
| Auto-saved session (hidden, one-shot, consumed on next launch) | `$XDG_DATA_HOME/terminator-criu/session.json` |
| Helper diagnostic log, transient state | `$XDG_STATE_HOME/terminator-criu/helper.log` |

`session.json` is written by the auto-save hooks and consumed (deleted)
at next startup after its layout has been handed to `create_layout`.
On consumption, any checkpoint dir whose UUID is not referenced by
the just-loaded session is treated as orphan and removed.

`$XDG_RUNTIME_DIR` would be the textbook choice for transient helper
state, but the helper's diagnostic log needs to survive across reboots
to be useful for post-mortem debugging — `/run` is a tmpfs and is
pruned of its problematic sub-mounts inside the namespace anyway.
`$XDG_STATE_HOME` (under `/home`) lives on persistent storage and
matches the XDG semantics for "data that persists between runs but
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

## Failure handling

Not every process can be checkpointed. CRIU bails on programs that use
features it doesn't understand (`io_uring` shared mappings, certain
exotic socket types), and even successful CRIU dumps can produce
restored processes that are alive at the kernel level but wedged at the
application level (notably JavaScript runtimes that swallowed an
unhandled Promise rejection during the dump/restore window). The
integration handles these gracefully rather than pretending nothing
happened.

**Dump-time failures (window close).** When the user closes a window,
each CRIU tab is checkpointed in turn. If any fail, a second dialog
appears listing the failing tab(s) and the underlying reason (the CRIU
log tail). Two buttons:

- **Cancel close** — veto the close. Tabs stay running; partial dump
  dirs from the failing tabs are wiped, so right-clicking a failing
  tab and unchecking "Checkpoint capable" leaves it in a clean state
  for next time.
- **Close anyway** — proceed. Successful tabs are restored normally
  on next launch; failing tabs get a `criu_restart` marker in
  `session.json` (with the original argv + cwd + foreground program
  name captured via `TIOCGPGRP`-on-master + `/proc/<pgid>/cmdline`).

The dialog has a **"Remember this choice"** checkbox; ticking it
before Close Anyway sets `close_anyway_on_checkpoint_failure=True` on
every distinct profile that had a failing tab, so the dialog is skipped
next time. Reversible from Preferences → Profile → Command.

**Restart-fresh path on next launch.** A `criu_restart` marker tells
the load path: re-launch the original program with its original argv,
in its original cwd, but as a fresh process (no CRIU state). If the
foreground at checkpoint time was a *child* of the shell (e.g. `codex`
running inside bash), the relaunch is wrapped as `shell -c 'prog;
exec shell'` — so when the program exits, the tab survives and the
user lands at a fresh shell instead of the tab closing entirely. POSIX
shell semantics, works for bash/zsh/fish/dash.

A small explanatory banner is fed into the VTE on top of the relaunched
program: "previous CRIU checkpoint could not be used — reason — original
command line was re-launched." Optional scrollback replay on this path
is off by default (see preferences) because the saved buffer belongs to
a *different* process than the one freshly launched, which can be
confusing.

**Restore-time failures.** If `criu restore` itself raises (Python-
detectable failure, distinct from "alive but wedged" — see limitations
below), the tab's `pending_restart` is synthesized from the saved
`criu_spawn_argv`/`criu_spawn_cwd` layout fields and the same banner +
restart-fresh path runs.

**Manual close of a wedged tab.** When the user right-clicks → Close
on a CRIU tab whose program isn't responding (e.g. a wedged restored
runtime that won't take Ctrl-C), `terminal.close()`:

1. Reads the foreground pgid from the master pty via `TIOCGPGRP`,
   sends SIGHUP to that pgrp (plus to the shell if it's not in the
   fg pgrp).
2. Polls `/proc` for those PIDs, pumping GLib events so the GUI stays
   responsive, until either everything exits or
   `graceful_kill_timeout_seconds` elapses.
3. Proceeds with the existing close cascade (shell SIGHUP + 500ms
   bash-history-loss safety wait + pty teardown).

If the grace period elapses without the program exiting, a non-modal
info dialog fires advising the user that this was probably a wedged
restore and recommending they uncheck "Checkpoint capable" on the
next tab that runs the same program. The dialog references the
specific program name and the active grace value, and points at the
preference to change or disable the behavior.

## Preferences reference

All per-profile, under **Preferences → Profile → Command**. The
cascade greyout reflects the dependency chain — child rows are
disabled when their parent is off.

| Setting | Default | Description |
|---|---|---|
| Enable checkpoint/restore (CRIU) for tabs in this profile | Off | Master switch. Without it, none of the rows below take effect. |
| Restart the original program if checkpoint or restore fails | On | Fall back to relaunching the original argv (with shell wrap) instead of dropping to the profile's default shell. |
| Restore scrollback history on checkpoint restore | On | Replay the captured VTE buffer when a CRIU restore succeeds. |
| Also replay scrollback when restarting after a failed checkpoint | Off | Replay scrollback on the *restart-fresh* path. Gated on the row above — scrollback belongs to a different process than the one being relaunched. |
| Always close anyway when checkpoints fail (skip the confirmation dialog) | Off | Auto-set by the dialog's Remember checkbox; surfaced here so the user can re-enable the prompt. |
| Grace period before forcibly killing tabs whose checkpoint failed (seconds, 0 = off) | 3 | SIGHUP foreground pgrp on close + wait up to this many seconds. Applies to both window-close (with failing tabs) and manual right-click close. 0 disables the SIGHUP-and-wait path entirely. |

## Known program-class limitations

Confirmed through testing of the integration. These are limitations of
CRIU and/or specific runtimes — the integration detects and degrades
gracefully but can't make them work:

- **OpenAI Codex CLI (Rust + io_uring)** — dump fails at the parse
  stage with `Unknown shit 600 (anon_inode:[io_uring])`. CRIU 4.2
  doesn't grok io_uring shared submission/completion queue mappings.
  The dump failure is clean (process unfrozen, source still running);
  the user sees the failure dialog. Restart-fresh on next launch
  works fine.
- **Claude Code (Bun runtime)** — dump succeeds. Restore succeeds
  mechanically: process tree intact, threads scheduling, TTY paired
  correctly, foreground program reading bytes from stdin. But the
  JS-side render reducer wedges, awaiting a Promise that depended on
  the captured-then-killed API socket. No kernel-layer fix is
  possible. The manual-close popup is designed to catch exactly this
  case post-mortem and recommend disabling checkpointing for the
  tab.
- **General pattern**: complex TUI apps with active streaming network
  state and modern async runtimes (Node, Bun, eventually anything
  using io_uring at scale) are fragile under c/r. Native shells,
  vim/emacs, less, ssh, REPLs (Python, Ruby, etc.) work reliably.

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

## Dev workflow note: helper sync

Edits to `libexec/terminator-criu-helper` do **not** take effect on
their own. The sudoers fragment only allows the installed binary at
`/usr/local/bin/terminator-criu-helper`, so `_find_helper()`
deliberately prefers the installed path over the source tree. After
every helper edit, sync to the installed path:

```
sudo cp libexec/terminator-criu-helper /usr/local/bin/terminator-criu-helper
```

If you installed via `setup.py install`, the helper also lives inside
the egg under `EGG-INFO/scripts/terminator-criu-helper`. Sync there
too if you're testing via `/usr/local/bin/terminator` rather than
`python3 ./terminator` from the source tree.

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
