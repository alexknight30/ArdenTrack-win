"""
ArdenTrack — AFK (away-from-keyboard) watcher.

Uses the Windows GetLastInputInfo API to detect idle time — the same
lightweight approach ActivityWatch uses.  No hooks, no per-pixel callbacks,
zero system overhead.
"""

import ctypes
import ctypes.wintypes
import logging
import threading
from datetime import datetime

from tzlocal import get_localzone

from ardentrack import db

logger = logging.getLogger(__name__)

_TZ = get_localzone()

AFK_THRESHOLD = 180   # seconds without input before user is AFK
WRITE_INTERVAL = 10   # seconds between DB writes

# ---------------------------------------------------------------------------
# Windows API: GetLastInputInfo
# ---------------------------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.wintypes.DWORD),
    ]


def _seconds_since_last_input() -> float:
    """Return seconds elapsed since the last keyboard/mouse input event."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now_ticks = ctypes.windll.kernel32.GetTickCount()
    elapsed_ms = (now_ticks - lii.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


class AFKWatcher:
    """Periodically checks idle time and writes AFK status events to the DB."""

    def __init__(self, stop_event: threading.Event):
        self._stop = stop_event
        self._last_written_afk = None

    def _write_loop(self):
        logger.info("AFK writer loop started")
        while not self._stop.is_set():
            self._stop.wait(WRITE_INTERVAL)
            if self._stop.is_set():
                break

            idle_s = _seconds_since_last_input()
            is_afk = idle_s > AFK_THRESHOLD

            if is_afk == self._last_written_afk:
                continue

            now_iso = datetime.now(tz=_TZ).isoformat()
            db.insert_event({
                "source": "afk",
                "timestamp": now_iso,
                "is_afk": 1 if is_afk else 0,
                "input_density": 0.0 if is_afk else 1.0,
            })

            self._last_written_afk = is_afk

        logger.info("AFK writer loop stopped")

    def run(self):
        logger.info("AFK watcher started")
        self._write_loop()
        logger.info("AFK watcher stopped")


def run(stop_event: threading.Event):
    """Entry point called by main.py thread launcher."""
    watcher = AFKWatcher(stop_event)
    watcher.run()
