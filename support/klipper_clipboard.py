"""Wait until Klipper's newest entry has actually reached the clipboard."""

import asyncio
from contextlib import suppress


CLIPBOARD_TIMEOUT_SECONDS = 2.0
CLIPBOARD_POLL_SECONDS = 0.050
# Klipper labels entries without text by their size, like "▨ 1920x1080".
IMAGE_LABEL_PREFIX = "▨ "


class KlipperClipboard:
    def __init__(self, bus):
        # Klipper is called by name, so a restarted plasmashell is still reachable.
        self.bus = bus

    async def wait_for_newest_entry(self):
        """Return once the clipboard holds Klipper's newest entry, or after a timeout."""
        # Klipper announces a selection before it has loaded the entry from disk.
        try:
            async with asyncio.timeout(CLIPBOARD_TIMEOUT_SECONDS):
                label = await self._newest_entry_label()
                while not label_matches(label, await read_clipboard_text()):
                    await asyncio.sleep(CLIPBOARD_POLL_SECONDS)
        except TimeoutError:
            # File entries are labelled with URLs that differ from their text.
            print("Clipboard does not match Klipper's newest entry; pasting anyway.", flush=True)

    async def _newest_entry_label(self):
        """Ask Klipper for the label of its newest history entry."""
        reply = asyncio.get_running_loop().create_future()

        def finished(label):
            if not reply.done():
                reply.set_result(str(label))

        def failed(error):
            if not reply.done():
                reply.set_exception(error)

        self.bus.call_async("org.kde.klipper", "/klipper", "org.kde.klipper.klipper",
                            "getClipboardContents", "", (), finished, failed)
        return await reply


def label_matches(label, text):
    """Compare Klipper's label with the clipboard's text, which is None without text."""
    if label.startswith(IMAGE_LABEL_PREFIX):
        return text is None
    return text == label


async def read_clipboard_text():
    """Return the clipboard's plain text, or None if it offers none."""
    process = await asyncio.create_subprocess_exec(
        "wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await process.communicate()
    finally:
        # A timeout can interrupt a clipboard owner that never finishes sending.
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    return output.decode(errors="replace") if process.returncode == 0 else None
