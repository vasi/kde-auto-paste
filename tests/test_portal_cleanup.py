import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import dbus

from auto_paste import AutoPaste, PASTE_KEY_EVENTS
from support.keyboard_portal import KeyboardPortal


class Subscription:
    def __init__(self, callback, signal_name, path):
        self.callback = callback
        self.signal_name = signal_name
        self.path = path
        self.active = True

    def remove(self):
        self.active = False


class Session:
    def __init__(self, bus, path):
        self.bus = bus
        self.path = path
        self.open = True

    def Close(self):
        self.bus.events.append(("close", self.path))
        self.open = False
        self.bus.emit("Closed", self.path)


class Portal:
    def __init__(self, bus):
        self.bus = bus
        self.restore_tokens = []

    def respond(self, options, results):
        path = f"/org/freedesktop/portal/desktop/request/1_42/{options['handle_token']}"
        self.bus.emit("Response", path, 0, results)

    def CreateSession(self, options, **callbacks):
        path = f"/org/freedesktop/portal/desktop/session/1_42/{options['session_handle_token']}"
        self.bus.sessions[path] = Session(self.bus, path)
        self.respond(options, {"session_handle": path})

    def SelectDevices(self, session, options, **callbacks):
        self.restore_tokens.append(options.get("restore_token", ""))
        self.respond(options, {})

    def Start(self, session, parent, options, **callbacks):
        self.respond(options, {"devices": 1, "restore_token": f"approval-{len(self.bus.sessions)}"})

    def NotifyKeyboardKeysym(self, session, options, keysym, pressed):
        if not self.bus.sessions[session].open:
            raise dbus.DBusException("session closed")
        self.bus.events.append(("key", int(keysym), bool(pressed)))


class Bus:
    def __init__(self):
        self.subscriptions = []
        self.sessions = {}
        self.events = []
        self.portal = Portal(self)

    def get_object(self, name, path):
        if path == "/org/freedesktop/portal/desktop":
            return self.portal
        return self.sessions[path]

    def get_unique_name(self):
        return ":1.42"

    def add_signal_receiver(self, callback, signal_name, path, **kwargs):
        subscription = Subscription(callback, signal_name, path)
        self.subscriptions.append(subscription)
        return subscription

    def emit(self, signal_name, path, *args):
        for subscription in list(self.subscriptions):
            if (subscription.active and subscription.signal_name == signal_name
                    and subscription.path == path):
                subscription.callback(*args)


class PortalCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.token_path = Path(temporary.name) / "restore_token"
        self.token_path.write_text("previous-approval")
        for patcher in (
            patch("support.keyboard_portal.dbus.Interface", side_effect=lambda obj, interface: obj),
            patch("support.keyboard_portal.approval_token_path", return_value=self.token_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bus = Bus()
        self.report_failure = Mock()
        self.portal = KeyboardPortal(self.bus, self.report_failure)
        self.app = AutoPaste.__new__(AutoPaste)
        self.app.keyboard_portal = self.portal
        self.app.klipper_popup_caption = "Clipboard Popup"
        self.app.active_window = {"internalId": "target", "resourceClass": "editor", "caption": "Test"}
        self.app.focus_changed = asyncio.Event()
        self.app.wait_for_target_to_settle = AsyncMock()
        self.source = {"internalId": "source", "resourceClass": "org.kde.plasmashell", "caption": "Clipboard Popup"}

    def assert_session_closed(self):
        self.assertTrue(self.bus.sessions)
        self.assertTrue(all(not session.open for session in self.bus.sessions.values()))
        self.assertFalse(any(subscription.active for subscription in self.bus.subscriptions))
        self.assertEqual(self.portal.held_keys, set())
        self.report_failure.assert_not_called()

    async def test_each_paste_closes_session_and_restores_saved_approval(self):
        for _ in range(2):
            await self.app.paste_selection(self.source)
            self.assert_session_closed()
        self.assertEqual(self.bus.portal.restore_tokens, ["previous-approval", "approval-1"])
        self.assertEqual(self.token_path.read_text(), "approval-2")
        self.assertEqual(len(self.bus.sessions), 2)
        self.assertEqual([event[1:] for event in self.bus.events if event[0] == "key"],
                         list(PASTE_KEY_EVENTS) * 2)

    async def test_focus_loss_releases_modifier_before_closing(self):
        send_key = self.portal.send_key

        def lose_focus(keysym, pressed):
            send_key(keysym, pressed)
            self.app.active_window = None

        self.portal.send_key = lose_focus
        await self.app.paste_selection(self.source)
        self.assert_session_closed()
        self.assertEqual(self.bus.events[:2], [("key", 0xFFE1, True), ("key", 0xFFE1, False)])
        self.assertEqual(self.bus.events[2][0], "close")

    async def test_cancellation_releases_modifier_and_closes_session(self):
        async def hold_modifier(target_id):
            self.portal.send_key(0xFFE1, True)
            raise asyncio.CancelledError

        self.app.send_paste_keys = hold_modifier
        with self.assertRaises(asyncio.CancelledError):
            await self.app.paste_selection(self.source)
        self.assert_session_closed()
        self.assertEqual(self.bus.events[:2], [("key", 0xFFE1, True), ("key", 0xFFE1, False)])

    async def test_failed_approval_closes_created_session(self):
        self.portal._select_keyboard = AsyncMock(side_effect=RuntimeError("permission cancelled"))
        with self.assertRaisesRegex(RuntimeError, "permission cancelled"):
            await self.app.paste_selection(self.source)
        self.assert_session_closed()

    async def test_unexpected_close_still_reports_failure(self):
        await self.portal.get_approval()
        session = next(iter(self.bus.sessions.values()))
        session.Close()
        self.report_failure.assert_called_once()
        self.assertEqual(str(self.report_failure.call_args.args[0]), "portal session closed")

    async def test_queued_close_from_previous_paste_does_not_stop_next_paste(self):
        await self.app.paste_selection(self.source)
        previous_closed = next(subscription.callback for subscription in self.bus.subscriptions
                               if subscription.signal_name == "Closed")
        await self.portal.get_approval()
        previous_closed()
        self.report_failure.assert_not_called()
        self.portal.close()
        self.portal.close()
        self.assert_session_closed()
        self.assertEqual(len([event for event in self.bus.events if event[0] == "close"]), 2)

    async def test_paste_error_releases_modifier_and_preserves_error(self):
        async def fail_paste(target_id):
            self.portal.send_key(0xFFE1, True)
            raise RuntimeError("paste failed")

        self.app.send_paste_keys = fail_paste
        with self.assertRaisesRegex(RuntimeError, "paste failed"):
            await self.app.paste_selection(self.source)
        self.assert_session_closed()
        self.assertEqual(self.bus.events[:2], [("key", 0xFFE1, True), ("key", 0xFFE1, False)])


if __name__ == "__main__":
    unittest.main()
