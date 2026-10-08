#!/usr/bin/env python3
"""Paste clipboard-source selections after focus returns to the destination."""

import asyncio
import configparser
from contextlib import suppress
import gettext
import json
import os
from pathlib import Path
import signal
import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.events import GLibEventLoop
from gi.repository import GLib, GLibUnix

from support.emoji_window_closer import EmojiWindowCloser
from support.keyboard_portal import KeyboardPortal
from support.kwin_scripts import KWinScripts


FOCUS_SETTLE_SECONDS = 0.050
KEY_INTERVAL_SECONDS = 0.030
# XKB keysyms: Shift_L and Insert. Keep modifier changes separate.
PASTE_KEY_EVENTS = ((0xFFE1, True), (0xFF63, True),
                    (0xFF63, False), (0xFFE1, False))


def resolve_future(future):
    """Complete a signal's awaitable result, ignoring duplicate notifications."""
    if not future.done():
        future.set_result(None)


def translated_klipper_caption():
    """Look up the popup title using Plasma's configured UI language."""
    # Plasma's UI language can differ from the process locale.
    config = configparser.ConfigParser(interpolation=None)
    config.read(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
                / "plasma-localerc")
    language = config.get("Translations", "LANGUAGE", fallback="")
    translation = gettext.translation("klipper", "/usr/share/locale",
                                      languages=language.split(":") if language else None,
                                      fallback=True)
    return translation.gettext("Clipboard Popup")


class AutoPaste(dbus.service.Object):
    """Connect clipboard selections and KWin focus notifications to a paste task."""

    def __init__(self, bus, name):
        super().__init__(name, "/ca/vasilevsky/AutoPaste")
        # Klipper shares Plasma's window class, so its caption identifies the popup.
        self.klipper_popup_caption = translated_klipper_caption()
        # Latest snapshot from focus.js; None when no window is active or known yet.
        self.active_window = None
        # Wakes focus waiters; active_window holds the current state, not this event.
        self.focus_changed = asyncio.Event()
        # One selection's coroutine, or None when ready for another selection.
        self.paste_task = None
        # Awaited by run(): normal completion means shutdown; an exception means failure.
        self.shutdown_result = asyncio.get_running_loop().create_future()
        # Handles portal approval and keyboard input.
        self.keyboard_portal = KeyboardPortal(bus, self.report_failure)
        # Loads and unloads the focus watcher and temporary emoji-close script.
        self.kwin_scripts = KWinScripts(bus)
        # Closes the picker through KWin; this class still handles focus afterward.
        self.emoji_closer = EmojiWindowCloser(name, self.kwin_scripts)
        bus.add_signal_receiver(
            self.on_clipboard_history_updated, signal_name="clipboardHistoryUpdated",
            dbus_interface="org.kde.klipper.klipper", bus_name="org.kde.klipper", path="/klipper")

    @dbus.service.method("ca.vasilevsky.AutoPaste", in_signature="s", out_signature="")
    def FocusChanged(self, properties):
        """Receive the active window snapshot from focus.js."""
        self.active_window = json.loads(str(properties))
        self.focus_changed.set()

    def on_clipboard_history_updated(self):
        """Treat a clipboard update while a source has focus as a selection."""
        if (self.shutdown_result.done() or self.paste_task is not None
                or not self.is_clipboard_source(self.active_window)):
            return
        # Ignore further selections until this paste finishes.
        self.paste_task = asyncio.create_task(self.paste_selection(self.active_window))
        self.paste_task.add_done_callback(self.on_paste_finished)

    def on_paste_finished(self, task):
        """Allow the next selection, or propagate this paste's failure to run()."""
        self.paste_task = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.report_failure(error)

    async def paste_selection(self, source_window):
        """Close the picker if needed, find the destination, and paste there."""
        try:
            if source_window["resourceClass"] == "org.kde.plasma.emojier":
                await self.emoji_closer.close(source_window["internalId"])

            target_window = await self.wait_for_focus(
                lambda window: window and not self.is_clipboard_source(window))
            target_id = target_window["internalId"]
            await self.keyboard_portal.get_approval()

            # Approval can steal focus; return to the destination chosen above.
            await self.wait_for_target_to_settle(target_id)
            await self.send_paste_keys(target_id)
        finally:
            # Focus changes, cancellation, or errors can interrupt a held modifier.
            # Close the session so KDE stops advertising its virtual touchscreen.
            self.keyboard_portal.close()

    async def wait_for_target_to_settle(self, target_id):
        """Wait until the destination stays focused for the full settling delay."""
        while True:
            await self.wait_for_focus(lambda window: self.is_window(window, target_id))
            self.focus_changed.clear()
            try:
                await asyncio.wait_for(self.focus_changed.wait(), FOCUS_SETTLE_SECONDS)
            except TimeoutError:
                if self.is_window(self.active_window, target_id):
                    return
            # A focus notification restarts the settling delay.

    async def send_paste_keys(self, target_id):
        """Send Shift+Insert, stopping if focus leaves the destination."""
        for keysym, pressed in PASTE_KEY_EVENTS:
            if not self.is_window(self.active_window, target_id):
                return
            self.keyboard_portal.send_key(keysym, pressed)
            await asyncio.sleep(KEY_INTERVAL_SECONDS)

    def is_clipboard_source(self, window):
        """Recognize Emojier by class and Klipper by class plus translated caption."""
        return bool(window and (
            window["resourceClass"] == "org.kde.plasma.emojier" or
            (window["resourceClass"] == "org.kde.plasmashell" and
             window["caption"] == self.klipper_popup_caption)))

    def is_window(self, window, window_id):
        """Compare KWin's window identity, allowing for no active window."""
        return bool(window and window["internalId"] == window_id)

    async def wait_for_focus(self, predicate):
        """Return the current window once it satisfies the caller's condition."""
        while not predicate(self.active_window):
            # The snapshot was just checked; wait for the next notification.
            self.focus_changed.clear()
            await self.focus_changed.wait()
        return self.active_window

    def report_failure(self, error):
        """Make a callback failure terminate the awaited application coroutine."""
        # Callback exceptions must reach run(), rather than just being logged by D-Bus.
        if not self.shutdown_result.done():
            self.shutdown_result.set_exception(error)


async def run():
    """Watch selections until a stop signal or a callback failure arrives."""
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName("ca.vasilevsky.AutoPaste", bus, do_not_queue=True)
    app = AutoPaste(bus, name)

    def on_stop_signal():
        resolve_future(app.shutdown_result)
        # GLib removes this signal handler when it returns False.
        return False

    for signum in (signal.SIGINT, signal.SIGTERM):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, on_stop_signal)
    try:
        with app.kwin_scripts.running("focus.js", "klipper-auto-paste", app.report_failure):
            print(f"Watching Klipper ({app.klipper_popup_caption!r}) and Emojier. Ctrl+C stops.", flush=True)
            await app.shutdown_result
    finally:
        # Let the paste's finally block release held keys before the process exits.
        if app.paste_task is not None:
            paste_task = app.paste_task
            paste_task.cancel()
            with suppress(asyncio.CancelledError):
                await paste_task


def main():
    """Run asyncio coroutines and GLib callbacks on the same event loop."""
    # This asyncio loop dispatches GLib sources, including D-Bus and signals.
    loop = GLibEventLoop(GLib.MainContext.default())
    try:
        with loop:
            return loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
