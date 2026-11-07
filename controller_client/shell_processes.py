"""OS shell and terminal process lists shared by close_app and the capture.

``app_closer_windows`` / ``app_closer_macos`` use them to refuse closing the
OS shell or the runner's terminal; ``window_capture_policy`` denies capturing
their windows; the Linux window inventories leave out the dock and desktop
windows of ``LINUX_SHELL_SURFACE_PROCESSES``. Plain data, no platform
imports, so every OS can import it.
"""

from __future__ import annotations

from typing import Final

# Windows: OS shell processes (exe names, lower case).
WINDOWS_OS_SHELL_EXES: Final[frozenset[str]] = frozenset(
    {
        "explorer.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "searchhost.exe",
        "searchapp.exe",
        "textinputhost.exe",
        "lockapp.exe",
        "dwm.exe",
        "csrss.exe",
        "winlogon.exe",
    }
)
# Windows: terminals whose tabs close with ctrl+shift+w rather than ctrl+w.
WINDOWS_TABBED_TERMINAL_EXES: Final[frozenset[str]] = frozenset({"windowsterminal.exe"})
# Windows: terminal hosts that may hold the runner's console without being its
# ancestor: Windows 11 hands a console started from Explorer or a shortcut to
# Windows Terminal, and GetConsoleWindow() then returns a pseudo-window. While
# the runner has a console, these are treated as hosting it.
WINDOWS_TERMINAL_HOST_EXES: Final[frozenset[str]] = frozenset(
    {
        "windowsterminal.exe",
        "openconsole.exe",
        "wezterm-gui.exe",
        "alacritty.exe",
        "tabby.exe",
        "hyper.exe",
        "conemu.exe",
        "conemu64.exe",
        "mintty.exe",
        "warp.exe",
    }
)

# macOS: OS shell apps (bundle ids).
MACOS_OS_SHELL_BUNDLE_IDS: Final[frozenset[str]] = frozenset(
    {
        "com.apple.finder",
        "com.apple.dock",
        "com.apple.loginwindow",
        "com.apple.systemuiserver",
        "com.apple.WindowManager",
        "com.apple.controlcenter",
    }
)
# macOS: terminals whose front tab's tty AppleScript can read.
MACOS_TTY_SCRIPT_TERMINAL_BUNDLE_IDS: Final[frozenset[str]] = frozenset(
    {"com.apple.Terminal", "com.googlecode.iterm2"}
)
# macOS: terminal apps that may host the runner without being its ancestor
# (iTerm2 3.4+ runs sessions under iTermServer, a child of launchd).
MACOS_TERMINAL_BUNDLE_IDS: Final[frozenset[str]] = frozenset(
    {
        *MACOS_TTY_SCRIPT_TERMINAL_BUNDLE_IDS,
        "dev.warp.Warp-Stable",
        "dev.warp.Warp",
        "org.alacritty",
        "net.kovidgoyal.kitty",
        "com.github.wez.wezterm",
        "com.mitchellh.ghostty",
        "co.zeit.hyper",
        "org.tabby",
    }
)

# Linux: shell, panel and desktop processes whose dock and desktop windows
# (``_NET_WM_WINDOW_TYPE_DOCK`` / ``_DESKTOP``, Mutter's DOCK / DESKTOP) the
# window inventories leave out. Any client may declare its window a dock or
# a desktop, so a window of such a type is left out only when its process
# (from its pid) is one of these; every other one is a non-capturable
# overlay. Normalized names (``protocol.normalize_process_name``), each at
# most 15 characters so it also matches ``/proc/<pid>/comm``. The GNOME
# helper extension keeps a copy (gnome_capture_helper/extension.js
# SHELL_SURFACE_PROCESSES): change both together.
LINUX_SHELL_SURFACE_PROCESSES: Final[frozenset[str]] = frozenset(
    {
        "plasmashell",
        "xfce4-panel",
        "xfdesktop",
        "gnome-shell",
        "mate-panel",
        "caja",
        "nemo-desktop",
        "cinnamon",
        "budgie-panel",
        "lxpanel",
        "lxqt-panel",
        "pcmanfm",
        "pcmanfm-qt",
        "tint2",
        "polybar",
        "xfce4-notifyd",
    }
)
