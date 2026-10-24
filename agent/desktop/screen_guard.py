"""No screen blanking or locking while the player plays (docs/09-exceptions.md RCV-006,
03 F20).

In the user's session: while a player job runs, ask the screen saver not to start
(``org.freedesktop.ScreenSaver.Inhibit``) and give that back when no job runs. Every
check also asks whether the screen is locked or blank anyway (``GetActive``): that is
told once (RCV-006, the cells get a warning) and cleared when it is back. Sleep is the
node's (SYS-005). ``Bus`` is the only part that talks D-Bus, so tests bring their own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

APP = "ZeroLoss Agent Desktop"
REASON = "A ZeroLoss test is playing video on this PC"


def answer(reply: Any, error_type: Any) -> bool | None:
    """GetActive's answer, or None when the call failed: an error reply carries its error
    text as the argument, which is not "locked" (GNOME answers the freedesktop GetActive
    with NotSupported — the whole UHD run of 2026-10-10 was told "locked")."""
    if reply.type() == error_type:
        return None
    args = reply.arguments()
    return bool(args[0]) if args else None


class Bus:
    """The session D-Bus screen saver (Qt D-Bus; the calls are short). Inhibit through
    the freedesktop name; GetActive there, else GNOME's own (it answers the question)."""

    def __init__(self) -> None:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

        session = QDBusConnection.sessionBus()
        self._iface = QDBusInterface("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                                     "org.freedesktop.ScreenSaver", session)  # fmt: skip
        self._gnome = QDBusInterface("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                     "org.gnome.ScreenSaver", session)  # fmt: skip
        self._error = QDBusMessage.MessageType.ErrorMessage

    @property
    def ok(self) -> bool:
        return bool(self._iface.isValid())

    def inhibit(self) -> int | None:
        reply = self._iface.call("Inhibit", APP, REASON)
        args = reply.arguments()
        return int(args[0]) if args else None

    def uninhibit(self, cookie: int) -> None:
        self._iface.call("UnInhibit", cookie)

    def active(self) -> bool | None:
        got = answer(self._iface.call("GetActive"), self._error)
        if got is None and self._gnome.isValid():
            got = answer(self._gnome.call("GetActive"), self._error)
        return got


class ScreenGuard:
    def __init__(self, bus: Any, running: Callable[[], bool],
                 tell: Callable[..., None]) -> None:  # fmt: skip
        self.bus, self._running, self._tell = bus, running, tell
        self.cookie: int | None = None
        self.locked = False

    def check(self) -> None:
        """Every 2 s from the window's timer (the calls are local and short)."""
        if not getattr(self.bus, "ok", False):
            return  # no screen saver on this desktop: nothing to hold
        playing = self._running()
        if playing and self.cookie is None:
            self.cookie = self.bus.inhibit()
        elif not playing and self.cookie is not None:
            self.bus.uninhibit(self.cookie)
            self.cookie = None
        locked = bool(playing and self.bus.active())
        if locked and not self.locked:
            self._tell("ZL-RCV-006", "warn",
                       "Screen locked or blanked on this PC — the player may not show")  # fmt: skip
        elif self.locked and not locked:
            self._tell("ZL-INFO", "info", "Screen is back", ["ZL-RCV-006"])
        self.locked = locked

    def release(self) -> None:
        if self.cookie is not None and getattr(self.bus, "ok", False):
            self.bus.uninhibit(self.cookie)
        self.cookie = None
