#!/usr/bin/env python3
"""Install this checkout as a Plasma user service, then start it."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


def quote_exec_argument(value):
    """Quote one systemd ExecStart argument, including literal % characters."""
    # ExecStart=: disables dollar expansion, but systemd still interprets these.
    escapes = {
        "\\": "\\\\",
        '"': '\\"',
        "%": "%%",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    escaped = "".join(escapes.get(character, character) for character in str(value))
    return f'"{escaped}"'


def main():
    script_path = Path(__file__).resolve().with_name("auto_paste.py")
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    unit_path = config_dir / "systemd/user/auto-paste.service"

    # Check dependencies with the same Python that will run the service.
    subprocess.run([
        sys.executable, "-c",
        "import dbus; from gi.events import GLibEventLoop; "
        "from gi.repository import GLib, GLibUnix",
    ], check=True)
    if shutil.which("wl-paste") is None:
        sys.exit("wl-paste not found; install wl-clipboard first.")

    python_argument = quote_exec_argument(sys.executable)
    script_argument = quote_exec_argument(script_path)
    unit = f"""[Unit]
Description=Paste selections from Klipper and Emoji Picker
PartOf=graphical-session.target
After=plasma-kwin_wayland.service plasma-plasmashell.service

[Service]
ExecStart=:{python_argument} {script_argument}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=plasma-workspace.target
"""
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "auto-paste.service"], check=True)
    # Restart also applies a changed checkout location when installing again.
    subprocess.run(["systemctl", "--user", "restart", "auto-paste.service"], check=True)

    print(f"Installed and started {unit_path}")
    print("Logs: journalctl --user -u auto-paste.service -f")
    print("Disable: systemctl --user disable --now auto-paste.service")


if __name__ == "__main__":
    main()
