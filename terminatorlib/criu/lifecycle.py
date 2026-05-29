# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""terminatorlib.criu.lifecycle - listen for system-level events that
should trigger an auto-checkpoint of all CRIU-active tabs.

Three DBus subscriptions:

  * SYSTEM bus / org.freedesktop.login1.Manager.PrepareForShutdown(true)
        Terminator is about to die. Dump all tabs, mark preserve,
        save session.

  * SYSTEM bus / org.freedesktop.login1.Manager.PrepareForSleep(true)
        System is suspending; terminator and its tabs will keep
        running on wake. Dump as a safety net (against power loss
        during sleep) without setting the preserve flag — a later
        clean exit still wipes the dump.

  * SESSION bus / org.freedesktop.ScreenSaver.ActiveChanged(true)
        The DE's screensaver/lock-screen activated. Same safety-net
        semantic as sleep: dump now in case of power loss; tabs
        keep running. Honored by KDE Plasma, GNOME, XFCE, etc.

To get logind to *wait* for us instead of killing terminator
mid-dump, we hold a `delay` inhibit lock on shutdown+sleep at all
times. When a signal arrives we do our work, then release the lock so
the system can proceed. If the signal carries `start=False` (the
shutdown was cancelled), we re-acquire so we're ready for next time.

All errors are swallowed and logged — losing any of these hooks
degrades to "no auto-checkpoint on that particular trigger", which
is annoying but not catastrophic. Especially: this module's
`install()` is safe to call on systems without systemd-logind or
without a screensaver service.
"""

import os

from ..util import dbg, err

try:
    import dbus
    import dbus.mainloop.glib
    _DBUS_AVAILABLE = True
except ImportError:
    _DBUS_AVAILABLE = False


_LOGIND_BUS = "org.freedesktop.login1"
_LOGIND_PATH = "/org/freedesktop/login1"
_LOGIND_IFACE = "org.freedesktop.login1.Manager"

# Cross-desktop screensaver. Plasma, GNOME, XFCE, Cinnamon all
# implement this interface. The signal fires on the session bus and
# is broadcast — we don't need to specify a path to match it.
_SCREENSAVER_IFACE = "org.freedesktop.ScreenSaver"
_SCREENSAVER_SIGNAL = "ActiveChanged"


class _LifecycleHook(object):
    """Singleton holder for the logind subscription. The class shape
    lets us keep the inhibit fd and the bus connection alive for the
    process lifetime without leaking them into module globals."""

    def __init__(self, terminator):
        self.terminator = terminator
        self.system_bus = None
        self.session_bus = None
        self.manager = None
        self.inhibit_fd = -1

    def install(self):
        if not _DBUS_AVAILABLE:
            dbg("criu.lifecycle: python-dbus not importable, skipping")
            return False
        # Reuse whatever Gtk main loop integration the rest of
        # terminator's DBus code already set up. Setting it again here
        # is harmless (DBusGMainLoop is idempotent).
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        # System bus → logind (shutdown / sleep). Not fatal if absent
        # (e.g. non-systemd environments).
        self._install_logind()

        # Session bus → screensaver (screen blank / lock). Not fatal
        # if absent (e.g. headless server, no DE running).
        self._install_screensaver()

        return True

    def _install_logind(self):
        try:
            self.system_bus = dbus.SystemBus()
            self.manager = dbus.Interface(
                self.system_bus.get_object(_LOGIND_BUS, _LOGIND_PATH),
                _LOGIND_IFACE,
            )
        except dbus.DBusException as e:
            dbg("criu.lifecycle: logind not reachable, skipping: %s" % e)
            return
        self._take_inhibit()
        try:
            self.manager.connect_to_signal(
                "PrepareForShutdown", self._on_prepare_for_shutdown)
            self.manager.connect_to_signal(
                "PrepareForSleep", self._on_prepare_for_sleep)
        except dbus.DBusException as e:
            err("criu.lifecycle: failed to subscribe to logind signals: %s" % e)
            self._release_inhibit()
            return
        dbg("criu.lifecycle: subscribed to logind PrepareForShutdown/Sleep")

    def _install_screensaver(self):
        try:
            self.session_bus = dbus.SessionBus()
            # Broadcast signal — we filter by interface only. Works
            # regardless of where the DE's screensaver service is
            # registered (path varies: /ScreenSaver on some, /org/
            # freedesktop/ScreenSaver on others).
            self.session_bus.add_signal_receiver(
                self._on_screensaver_active_changed,
                signal_name=_SCREENSAVER_SIGNAL,
                dbus_interface=_SCREENSAVER_IFACE,
            )
        except dbus.DBusException as e:
            dbg("criu.lifecycle: session bus / screensaver unreachable: %s" % e)
            return
        dbg("criu.lifecycle: subscribed to screensaver ActiveChanged")

    def _take_inhibit(self):
        if self.manager is None or self.inhibit_fd != -1:
            return
        try:
            # 'delay' mode — let logind hold for us briefly, don't
            # outright block. Combined what="shutdown:sleep" so one fd
            # covers both events.
            fd = self.manager.Inhibit(
                "shutdown:sleep",
                "Terminator-CRIU",
                "Checkpointing tabs before suspend or shutdown",
                "delay",
            )
            # python-dbus returns a UnixFd wrapper; take() yields the
            # int fd and transfers ownership to us.
            self.inhibit_fd = fd.take()
            dbg("criu.lifecycle: inhibit fd %d held" % self.inhibit_fd)
        except dbus.DBusException as e:
            err("criu.lifecycle: failed to acquire inhibit lock: %s" % e)

    def _release_inhibit(self):
        if self.inhibit_fd != -1:
            try:
                os.close(self.inhibit_fd)
            except OSError:
                pass
            dbg("criu.lifecycle: inhibit fd %d released" % self.inhibit_fd)
            self.inhibit_fd = -1

    def _on_prepare_for_shutdown(self, starting):
        if bool(starting):
            dbg("criu.lifecycle: PrepareForShutdown(true) — checkpointing")
            try:
                self.terminator.criu_checkpoint_all_tabs(preserve_on_exit=True)
            except Exception as e:
                err("criu.lifecycle: checkpoint-on-shutdown failed: %s" % e)
            # Let logind proceed.
            self._release_inhibit()
        else:
            # Shutdown was cancelled (rare). Re-acquire so we're ready
            # for the next attempt.
            dbg("criu.lifecycle: PrepareForShutdown(false) — re-arming")
            self._take_inhibit()

    def _on_prepare_for_sleep(self, starting):
        if bool(starting):
            dbg("criu.lifecycle: PrepareForSleep(true) — safety-checkpoint")
            try:
                self.terminator.criu_checkpoint_all_tabs(preserve_on_exit=False)
            except Exception as e:
                err("criu.lifecycle: checkpoint-on-sleep failed: %s" % e)
            self._release_inhibit()
        else:
            # System resumed from sleep. Tabs are still alive; re-take
            # the lock so the next sleep event also waits for us.
            dbg("criu.lifecycle: PrepareForSleep(false) — woken up, re-arming")
            self._take_inhibit()

    def _on_screensaver_active_changed(self, active):
        """Fired by KDE Plasma / GNOME / XFCE etc. when the
        screensaver activates or deactivates. We only act on the
        activation edge — there's no point checkpointing again when
        the user unlocks."""
        if not bool(active):
            return
        dbg("criu.lifecycle: screensaver active — safety-checkpoint")
        try:
            self.terminator.criu_checkpoint_all_tabs(preserve_on_exit=False)
        except Exception as e:
            err("criu.lifecycle: checkpoint-on-screensaver failed: %s" % e)


_HOOK = None


def install(terminator):
    """Wire up the logind listeners. Safe to call once at startup;
    no-op if already installed. Returns True on success, False on
    skip (e.g. logind not available)."""
    global _HOOK
    if _HOOK is not None:
        return True
    hook = _LifecycleHook(terminator)
    if not hook.install():
        return False
    _HOOK = hook
    return True
