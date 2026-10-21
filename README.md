# kde-auto-paste

KDE is wonderful and includes tons of features, like:

* Klipper, a clipboard history manager
* Emojier, an emoji selection window

However, neither of these pastes the result when you select it. Instead, they just put it on the clipboard, and you have to paste it yourself.

This tool fixes that! Once it's installed, it watches for selections, and auto-pastes them.

# Installation

```bash
cd SOME_DIRECTORY
git clone https://github.com/vasi/kde-auto-paste
./kde-auto-paste/install.py
```

Then use Klipper or Emojier the way you normally would. The first time it auto-pastes, it will request approval, but it shouldn't need that afterwards.

Requirements:

* KDE Plasma on Wayland
* dbus-python and PyGObject. These are already installed on most distros.
* wl-clipboard, used to check that your selection reached the clipboard before pasting.

Notes:

* You'll need to keep the cloned directory in place, so put it somewhere you don't mind.
* This closes Emojier as soon as you make a selection, so it can auto-paste it. That's what I want, but it may not be what you expect.

# Credits

* Inspired by [kde-klipper-paste-on-click](https://github.com/defue/kde-klipper-paste-on-click), but has some advantages:
  * Doesn't require kdotool.
  * Doesn't require ydotool, which requires scary permissions. Instead uses a desktop portal.
  * Handles not just Klipper, but Emojier too.
  * Works with all Klipper history entries, even the first.
* Written with significant LLM help, but heavily guided and reviewed. I hope this doesn't feel like slop!
* KDE's many contributors built this amazing desktop, which made this tool possible.
