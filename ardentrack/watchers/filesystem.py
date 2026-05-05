"""
ArdenTrack — Filesystem watcher.

Monitors user-configured matter folders via watchdog.  On file create/modify
events, writes a filesystem event to the database with the corresponding
matter hint.  Config is re-read every 60 seconds to pick up new folders.
"""

import fnmatch
import json
import logging
import os
import threading
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from tzlocal import get_localzone

from ardentrack import db
from ardentrack.paths import USERDATA_DIR

logger = logging.getLogger(__name__)

_TZ = get_localzone()

CONFIG_REFRESH_INTERVAL = 60  # seconds

IGNORED_PATTERNS = ["*.tmp", "~$*", "*.lock"]


def _profile_path():
    return os.path.join(USERDATA_DIR, "profile.json")


def _load_watched_folders():
    """Load watched_folders from profile.json. Returns list of {path, matter_name}."""
    path = _profile_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
        return profile.get("watched_folders", [])
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read profile.json: %s", exc)
        return []


def _is_ignored(file_path: str) -> bool:
    basename = os.path.basename(file_path)
    for pattern in IGNORED_PATTERNS:
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


class _MatterEventHandler(FileSystemEventHandler):

    def __init__(self, matter_name: str):
        super().__init__()
        self.matter_name = matter_name

    def _handle(self, event):
        if event.is_directory:
            return
        if _is_ignored(event.src_path):
            return

        event_type = "modified" if event.event_type == "modified" else "created"
        now_iso = datetime.now(tz=_TZ).isoformat()

        db.insert_event({
            "source": "filesystem",
            "timestamp": now_iso,
            "file_path": event.src_path,
            "file_event_type": event_type,
            "matter_hint": self.matter_name,
            "matter_hint_source": "filesystem",
            "matter_hint_confidence": 0.95,
        })

    def on_created(self, event):
        self._handle(event)

    def on_modified(self, event):
        self._handle(event)


class FilesystemWatcher:

    def __init__(self, stop_event: threading.Event):
        self._stop = stop_event
        self._observers: list[Observer] = []
        self._current_config: list[dict] = []

    def _start_observers(self, folders: list[dict]):
        self._stop_observers()
        self._current_config = folders

        for entry in folders:
            folder_path = entry.get("path", "")
            matter_name = entry.get("matter_name", "")
            if not folder_path or not os.path.isdir(folder_path):
                logger.warning("Skipping invalid watched folder: %s", folder_path)
                continue

            handler = _MatterEventHandler(matter_name)
            observer = Observer()
            observer.schedule(handler, folder_path, recursive=True)
            observer.daemon = True
            observer.start()
            self._observers.append(observer)
            logger.info("Watching folder %s for matter '%s'", folder_path, matter_name)

    def _stop_observers(self):
        for obs in self._observers:
            try:
                obs.stop()
                obs.join(timeout=5)
            except Exception:
                logger.exception("Error stopping observer")
        self._observers.clear()

    def run(self):
        logger.info("Filesystem watcher started")

        while not self._stop.is_set():
            folders = _load_watched_folders()

            if folders != self._current_config:
                if folders:
                    logger.info("Filesystem watcher config updated — %d folders", len(folders))
                    self._start_observers(folders)
                else:
                    logger.debug("No watched folders configured")
                    self._stop_observers()
                    self._current_config = []

            self._stop.wait(CONFIG_REFRESH_INTERVAL)

        self._stop_observers()
        logger.info("Filesystem watcher stopped")


def run(stop_event: threading.Event):
    """Entry point called by main.py thread launcher."""
    watcher = FilesystemWatcher(stop_event)
    watcher.run()
