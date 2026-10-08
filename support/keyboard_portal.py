"""Obtain keyboard permission and send keys through the desktop portal."""

import asyncio
import os
from pathlib import Path
import tempfile
import uuid

import dbus


KEYBOARD_DEVICE = 1
PERSIST_UNTIL_REVOKED = 2


class KeyboardPortal:
    def __init__(self, bus, report_failure):
        # The session bus carries requests and responses to the desktop portal.
        self.bus = bus
        # Notify the application if the portal ends our keyboard connection.
        self.report_failure = report_failure
        # D-Bus interface for requesting access and sending keyboard input.
        self.remote_desktop_portal = dbus.Interface(
            bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"),
            "org.freedesktop.portal.RemoteDesktop")
        # Track the session during approval too, so failures can close it.
        self.session = None
        self.session_closed_subscription = None
        # Keys pressed without a matching release; interruption must release them.
        self.held_keys = set()

    async def get_approval(self):
        """Obtain keyboard access, reusing the current session or saved approval."""
        if self.session is not None:
            return
        print("Requesting keyboard access (or restoring saved approval)...", flush=True)
        self.session = await self._create_session()
        self._watch_session_closed(self.session)
        await self._select_keyboard(self.session)
        approval = await self._start_session(self.session)
        self._save_approval(approval)
        print("Keyboard access granted; waiting for the target window.", flush=True)

    async def _create_session(self):
        """Create a portal connection; this step does not grant permission yet."""
        random_token = "paste_" + uuid.uuid4().hex
        result = await self._request_and_wait_for_response(
            "CreateSession", (), {"session_handle_token": random_token})
        return dbus.ObjectPath(result["session_handle"])

    def _watch_session_closed(self, session):
        """Report if the portal closes the connection, including during approval."""
        def closed(*args):
            if self.session == session:
                self.report_failure(RuntimeError("portal session closed"))

        self.session_closed_subscription = self.bus.add_signal_receiver(
            closed, signal_name="Closed",
            dbus_interface="org.freedesktop.portal.Session", path=session)

    async def _select_keyboard(self, session):
        """Ask for keyboard input and request that approval be remembered."""
        options = {"types": dbus.UInt32(KEYBOARD_DEVICE),
                   "persist_mode": dbus.UInt32(PERSIST_UNTIL_REVOKED)}
        token = read_approval_token()
        if token:
            options["restore_token"] = token
        await self._request_and_wait_for_response("SelectDevices", (session,), options)

    async def _start_session(self, session):
        """Request approval (or restore it), then verify keyboard access was granted."""
        approval = await self._request_and_wait_for_response("Start", (session, ""), {})
        if not int(approval.get("devices", 0)) & KEYBOARD_DEVICE:
            raise RuntimeError("keyboard access was not granted")
        return approval

    def _save_approval(self, approval):
        """Save the portal's replacement token for the next program run."""
        save_approval_token(approval_token_path(), approval.get("restore_token"))

    def send_key(self, keysym, pressed):
        """Send one press or release. keysym is an XKB key identifier."""
        self.remote_desktop_portal.NotifyKeyboardKeysym(
            self.session, dbus.Dictionary({}, signature="sv"),
            dbus.Int32(keysym), dbus.UInt32(int(pressed)))
        if pressed:
            self.held_keys.add(keysym)
        else:
            self.held_keys.discard(keysym)

    def release_held_keys(self):
        """Release unmatched presses after completion, interruption, or shutdown."""
        for keysym in list(self.held_keys):
            try:
                self.send_key(keysym, False)
            except dbus.DBusException:
                pass

    def close(self):
        """Release held keys and remove this paste's virtual input device."""
        self.release_held_keys()
        session = self.session
        self.session = None
        self.held_keys.clear()
        if self.session_closed_subscription is not None:
            self.session_closed_subscription.remove()
            self.session_closed_subscription = None
        if session is not None:
            try:
                dbus.Interface(
                    self.bus.get_object("org.freedesktop.portal.Desktop", session),
                    "org.freedesktop.portal.Session").Close()
            except dbus.DBusException:
                # The portal may have already closed the session.
                pass

    async def _request_and_wait_for_response(self, method, arguments, options):
        """Translate the portal's separate Response signal into an awaitable result."""
        request_token = "paste_" + uuid.uuid4().hex
        options = {**options, "handle_token": request_token}
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        # Choose the response path so we can listen before sending the request.
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{request_token}"
        request_future = asyncio.get_running_loop().create_future()

        def failed(error):
            if not request_future.done():
                request_future.set_exception(error)

        def response(code, results):
            if request_future.done():
                return
            if code:
                failed(RuntimeError("permission request cancelled" if code == 1 else
                                    "permission request failed"))
            else:
                request_future.set_result(results)

        # Listen first, so even an immediate response cannot be missed.
        response_subscription = self.bus.add_signal_receiver(
            response, signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request", path=request_path)
        try:
            # The method reply only returns a request handle. The signal has the result.
            getattr(self.remote_desktop_portal, method)(
                *arguments, dbus.Dictionary(options, signature="sv"),
                reply_handler=lambda handle: None, error_handler=failed)
            return await request_future
        finally:
            # Requests finish while the program continues; stop listening to each one.
            response_subscription.remove()


def read_approval_token():
    """Return saved approval, or an empty string on the first run."""
    try:
        return approval_token_path().read_text().strip()
    except FileNotFoundError:
        return ""


def approval_token_path():
    """Locate the cached token used to restore approval across program runs."""
    return (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
            / "kde-auto-paste" / "restore_token")


def save_approval_token(path, token):
    """Replace the saved token, or remove it if the portal did not issue one."""
    if not token:
        path.unlink(missing_ok=True)
        print("Portal did not save approval; it will ask again next run.", flush=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(str(token))
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
