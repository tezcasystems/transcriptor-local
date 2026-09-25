"""System-wide hotkey that works while another app (Teams) has focus.

Uses Win32 RegisterHotKey, which claims only this key combination; it does not
hook or observe any other keystroke. Tries several combinations because other
apps may already own one (on the author's laptop Ctrl+Alt+M was taken).
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
_VK_M, _VK_SPACE = 0x4D, 0x20
_WM_HOTKEY, _WM_QUIT = 0x0312, 0x0012
_PM_NOREMOVE = 0x0000
_HOTKEY_ID = 1
_PROBE_ID = 2

CANDIDATES = [
    ("Ctrl+Alt+M", _MOD_CONTROL | _MOD_ALT, _VK_M),
    ("Ctrl+Alt+Espacio", _MOD_CONTROL | _MOD_ALT, _VK_SPACE),
    ("Ctrl+Shift+Alt+M", _MOD_CONTROL | _MOD_SHIFT | _MOD_ALT, _VK_M),
]


def probe_hotkey() -> Optional[str]:
    """Label of the first combination currently free, without keeping it."""
    user32 = ctypes.windll.user32
    for label, mods, vk in CANDIDATES:
        if user32.RegisterHotKey(None, _PROBE_ID, mods | _MOD_NOREPEAT, vk):
            user32.UnregisterHotKey(None, _PROBE_ID)
            return label
    return None


class GlobalHotkey:
    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.label = ""

    @property
    def registered(self) -> bool:
        return bool(self.label)

    def start(self) -> Optional[str]:
        """Registers the first free combination; returns its label, or None if all are taken."""
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hotkey")
        self._thread.start()
        self._ready.wait(timeout=2)
        return self.label or None

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0
        self.label = ""

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        # Creates this thread's message queue so stop() can post WM_QUIT to it.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_NOREMOVE)
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for label, mods, vk in CANDIDATES:
            if user32.RegisterHotKey(None, _HOTKEY_ID, mods | _MOD_NOREPEAT, vk):
                self.label = label
                break
        self._ready.set()
        if not self.label:
            return
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY:
                    try:
                        self._callback()
                    except Exception:  # noqa: BLE001 - a failing callback must not kill the listener
                        pass
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
