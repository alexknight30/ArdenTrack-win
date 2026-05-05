"""
ArdenTrack — Window watcher.

Polls the foreground window every 2 seconds using win32gui and psutil.
Short-circuits before any system calls when the window hasn't changed.
"""

import logging
import threading
from datetime import datetime

import psutil
import win32gui
import win32process
from tzlocal import get_localzone

from ardentrack import db

logger = logging.getLogger(__name__)

_TZ = get_localzone()

POLL_INTERVAL = 2       # seconds between polls
FLUSH_INTERVAL = 30     # checkpoint: flush to DB even if window hasn't changed
MAX_EVENT_DURATION = 180  # 3 minutes — matches AFK threshold; discards sleep gaps

SYSTEM_PROCESSES = {
    "explorer.exe",
    "searchhost.exe",
    "shellexperiencehost.exe",
    "lockapp.exe",
    "startmenuexperiencehost.exe",
    "systemsettings.exe",
    "textinputhost.exe",
    "applicationframehost.exe",
    "searchui.exe",
    "cortana.exe",
    "taskmgr.exe",
    "dwm.exe",
    "csrss.exe",
    "winlogon.exe",
    "logonui.exe",
}

# Cache proc.exe() per PID — resolved once, reused forever
_exe_cache: dict[int, str] = {}


class WindowWatcher:
    """Continuously tracks the foreground window and writes events to the DB."""

    def __init__(self, stop_event: threading.Event):
        self._stop = stop_event
        self._prev_event = None
        self._prev_hwnd = None
        self._prev_title = None

    def _flush_prev(self):
        """Write the accumulated event to the database and clear it."""
        if self._prev_event is None:
            return
        ev = self._prev_event
        now = datetime.now(tz=_TZ)
        ev["duration_s"] = (now - ev["_start_dt"]).total_seconds()
        if ev["duration_s"] < 0.5:
            return
        if ev["duration_s"] > MAX_EVENT_DURATION:
            logger.warning(
                "Discarding window event spanning %.0fs (likely sleep gap): %s",
                ev["duration_s"], ev.get("window_title", "")[:60],
            )
            self._prev_event = None
            self._prev_hwnd = None
            self._prev_title = None
            return
        db.insert_event({
            "source": "window",
            "timestamp": ev["timestamp"],
            "duration_s": round(ev["duration_s"], 2),
            "app_name": ev["app_name"],
            "window_title": ev["window_title"],
            "process_path": ev["process_path"],
            "document_path": None,
        })
        self._prev_event = None

    def _checkpoint(self):
        """
        If the current event has been accumulating for >= FLUSH_INTERVAL seconds,
        flush it to DB and immediately start a new event for the same window.
        """
        if self._prev_event is None:
            return
        ev = self._prev_event
        now = datetime.now(tz=_TZ)
        elapsed = (now - ev["_start_dt"]).total_seconds()
        if elapsed > MAX_EVENT_DURATION:
            logger.warning(
                "Checkpoint: discarding stale event spanning %.0fs (likely sleep gap): %s",
                elapsed, ev.get("window_title", "")[:60],
            )
            self._prev_event = None
            self._prev_hwnd = None
            self._prev_title = None
            return
        if elapsed < FLUSH_INTERVAL:
            return

        self._flush_prev()
        self._prev_event = {
            "app_name": ev["app_name"],
            "window_title": ev["window_title"],
            "process_path": ev["process_path"],
            "timestamp": now.isoformat(),
            "_start_dt": now,
            "duration_s": 0,
        }

    def _poll(self):
        """Single poll tick — read the foreground window and update state."""
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return

            title = win32gui.GetWindowText(hwnd)

            # Short-circuit: if HWND and title are identical, nothing changed.
            # No psutil calls, no datetime, no system calls at all.
            if hwnd == self._prev_hwnd and title == self._prev_title:
                self._checkpoint()
                return

            _, pid = win32process.GetWindowThreadProcessId(hwnd)

            try:
                proc = psutil.Process(pid)
                with proc.oneshot():
                    app_name = proc.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return

            if not app_name:
                return
            if app_name.lower() in SYSTEM_PROCESSES:
                return

            # Resolve exe path once per PID, then cache
            process_path = _exe_cache.get(pid)
            if process_path is None:
                try:
                    process_path = proc.exe()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    process_path = ""
                _exe_cache[pid] = process_path

            now = datetime.now(tz=_TZ)

            self._flush_prev()

            self._prev_hwnd = hwnd
            self._prev_title = title
            self._prev_event = {
                "app_name": app_name,
                "window_title": title,
                "process_path": process_path,
                "timestamp": now.isoformat(),
                "_start_dt": now,
                "duration_s": 0,
            }

        except Exception:
            logger.exception("Error in window watcher poll")

    def run(self):
        """Main loop — polls until stop_event is set."""
        logger.info("Window watcher started")
        while not self._stop.is_set():
            self._poll()
            self._stop.wait(POLL_INTERVAL)
        self._flush_prev()
        logger.info("Window watcher stopped")


def run(stop_event: threading.Event):
    """Entry point called by main.py thread launcher."""
    watcher = WindowWatcher(stop_event)
    watcher.run()
