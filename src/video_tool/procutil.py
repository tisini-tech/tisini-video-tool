"""Helpers for starting helper programs (yt-dlp, FFmpeg, Deno) quietly.

The Windows build of the GUI has no console of its own. When such a program
starts a console program such as ``yt-dlp.exe``, Windows opens a black
command window for it unless the program is told not to. These helpers add
that instruction, and clean up the environment a frozen app passes on.

On a normal (not frozen) Linux or macOS run every helper here returns an
empty result, so development behaviour does not change.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010

# Variables a PyInstaller one-file program sets for its own use. A second
# one-file program started from it can mistake them for its own and fail
# to unpack.
_PYINSTALLER_VARS = (
    "_MEIPASS2",
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_ARCHIVE_FILE",
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_SPLASH_IPC",
)


def quiet_window_kwargs() -> dict:
    """Return ``subprocess`` keyword arguments that hide the child window.

    The result is empty except on Windows.
    """

    if not IS_WINDOWS:
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    return {
        "creationflags": CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
        # A window-less app has no usable standard input to share.
        "stdin": subprocess.DEVNULL,
    }


def child_env() -> dict | None:
    """Return a clean environment for a child program, or None.

    None means "inherit the current environment", which is right unless
    this program is a frozen (PyInstaller) build.
    """

    if not getattr(sys, "frozen", False):
        return None

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _PYINSTALLER_VARS
    }

    # PyInstaller points the library search path at its own folder and saves
    # the original. Put the original back so the child loads system libraries.
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        saved = env.pop(f"{name}_ORIG", None)
        if saved is not None:
            env[name] = saved
        elif name in env:
            del env[name]

    return env


def child_process_kwargs() -> dict:
    """Return every keyword argument a child process should get."""

    kwargs = quiet_window_kwargs()
    env = child_env()

    if env is not None:
        kwargs["env"] = env

    return kwargs


def install_no_window_default() -> None:
    """Make every ``subprocess.Popen`` in this program start without a window.

    This covers code that Video Tool does not control, such as yt-dlp
    starting FFmpeg. It does nothing outside Windows, and it is safe to call
    more than once.
    """

    if not IS_WINDOWS:
        return

    if getattr(subprocess.Popen, "_video_tool_no_window", False):
        return

    original_init = subprocess.Popen.__init__

    @functools.wraps(original_init)
    def patched_init(self, *args, **kwargs):
        # creationflags is the 14th positional argument of Popen.
        if len(args) < 14:
            flags = kwargs.get("creationflags", 0)
            if not flags & (
                CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_CONSOLE
            ):
                kwargs["creationflags"] = flags | CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched_init
    subprocess.Popen._video_tool_no_window = True
