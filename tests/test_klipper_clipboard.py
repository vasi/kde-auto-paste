import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from support import klipper_clipboard
from support.klipper_clipboard import KlipperClipboard, label_matches, read_clipboard_text


class Bus:
    def __init__(self, label):
        self.label = label
        self.calls = []

    def call_async(self, bus_name, path, interface, method, signature, args, reply_handler, error_handler):
        self.calls.append((bus_name, path, interface, method))
        reply_handler(self.label)


class LabelMatchesTest(unittest.TestCase):
    def test_text_entry_needs_identical_text(self):
        self.assertTrue(label_matches("new", "new"))
        self.assertFalse(label_matches("new", "old"))
        self.assertFalse(label_matches("new", None))

    def test_image_entry_needs_clipboard_without_text(self):
        self.assertTrue(label_matches("▨ 1920x1080", None))
        self.assertFalse(label_matches("▨ 1920x1080", "old"))


@patch.object(klipper_clipboard, "CLIPBOARD_POLL_SECONDS", 0)
class WaitForNewestEntryTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_until_klipper_entry_reaches_clipboard(self):
        bus = Bus("new")
        read = AsyncMock(side_effect=["old", None, "new"])
        with patch.object(klipper_clipboard, "read_clipboard_text", read):
            await KlipperClipboard(bus).wait_for_newest_entry()
        self.assertEqual(read.await_count, 3)
        self.assertEqual(bus.calls, [("org.kde.klipper", "/klipper", "org.kde.klipper.klipper",
                                      "getClipboardContents")])

    async def test_gives_up_after_timeout(self):
        read = AsyncMock(return_value="file:///old")
        with (patch.object(klipper_clipboard, "read_clipboard_text", read),
              patch.object(klipper_clipboard, "CLIPBOARD_TIMEOUT_SECONDS", 0.05),
              patch("builtins.print") as log):
            await KlipperClipboard(Bus("file:///new")).wait_for_newest_entry()
        log.assert_called_once()


class ReadClipboardTextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.wl_paste = Path(directory.name) / "wl-paste"
        self.fake_wl_paste('printf %s "$FAKE_CLIPBOARD"\nexit "$FAKE_STATUS"')
        environment = {"PATH": f"{directory.name}:{os.environ['PATH']}"}
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_wl_paste(self, script):
        self.wl_paste.write_text(f"#!/bin/sh\n{script}\n")
        self.wl_paste.chmod(0o755)

    async def test_returns_text_without_added_newline(self):
        with patch.dict(os.environ, {"FAKE_CLIPBOARD": "😀", "FAKE_STATUS": "0"}):
            self.assertEqual(await read_clipboard_text(), "😀")

    async def test_returns_none_without_text(self):
        with patch.dict(os.environ, {"FAKE_CLIPBOARD": "", "FAKE_STATUS": "1"}):
            self.assertIsNone(await read_clipboard_text())

    async def test_timeout_stops_a_stuck_transfer(self):
        self.fake_wl_paste("echo $$ > \"$0.pid\"\nexec tail -f /dev/null")
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(read_clipboard_text(), 0.5)
        pid = int(Path(f"{self.wl_paste}.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
