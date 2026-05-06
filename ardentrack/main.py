"""
ArdenTrack — Main entry point.

Initialises the database, configures logging, launches all watcher and
classifier threads, and keeps the process alive until a clean shutdown
signal is received.  Crashed threads are automatically restarted after
a 10-second delay.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env"))

from filelock import FileLock, Timeout

from ardentrack.paths import DATA_DIR, LOG_PATH, LOCK_PATH, BASE_DIR
from ardentrack import auth
from ardentrack import auth_server
from ardentrack import db
from ardentrack import supabase_sync
from ardentrack.watchers import window, afk, filesystem
from ardentrack.classifier import classify

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging():
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=2,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if os.environ.get("ARDEN_DEBUG") else logging.INFO)
    root.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)


logger = None  # initialised after _setup_logging()

# ---------------------------------------------------------------------------
# Thread management
# ---------------------------------------------------------------------------

RESTART_DELAY = 10  # seconds before restarting a crashed thread
SUPABASE_PULL_INTERVAL_SEC = 30 * 60
AUTH_WAIT_SEC = 300


def _resilient_target(name: str, target_fn, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            logger.info("Starting thread: %s", name)
            target_fn(stop_event)
        except Exception:
            if stop_event.is_set():
                break
            logger.exception("Thread '%s' crashed — restarting in %ds", name, RESTART_DELAY)
            stop_event.wait(RESTART_DELAY)
        else:
            break
    logger.info("Thread '%s' exited", name)


def main():
    global logger

    _setup_logging()
    logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Lockfile — prevent duplicate processes
    # ------------------------------------------------------------------
    lock = FileLock(LOCK_PATH, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        logger.error("ArdenTrack is already running (lockfile held). Exiting.")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("ArdenTrack starting")
    logger.info("Base dir: %s", BASE_DIR)
    logger.info("Data dir: %s", DATA_DIR)
    logger.info("=" * 60)

    db.setup()

    try:
        db.insert_telemetry(
            "app_open",
            metadata=json.dumps({"source": "ardentrack"}),
        )
    except Exception as e:
        logger.warning("Telemetry app_open insert skipped: %s", e)

    logger.info("SUPABASE_URL = %s", os.environ.get("SUPABASE_URL", "<not set>"))
    logger.info("SUPABASE_ANON_KEY = %s", "set (%d chars)" % len(os.environ.get("SUPABASE_ANON_KEY", "")) if os.environ.get("SUPABASE_ANON_KEY") else "<not set>")

    token_received_event = threading.Event()
    if not auth.has_credentials():
        logger.info("No stored auth credentials — starting auth listener on localhost")
        auth_server.start_auth_listener(token_received_event)
        if not token_received_event.wait(timeout=AUTH_WAIT_SEC):
            logger.warning(
                "No auth credentials within %ds — continuing in offline-only mode",
                AUTH_WAIT_SEC,
            )
    else:
        logger.info("Found existing auth credentials in keyring")

    tok = auth.get_valid_token()
    if tok:
        logger.info("Auth token valid — starting initial Supabase sync")
        try:
            supabase_sync.pull_matters()
            supabase_sync.pull_clients()
            supabase_sync.pull_profile()
            supabase_sync.pull_billing_status()
            supabase_sync.push_unsynced_entries()
            logger.info("Initial Supabase sync complete")
        except Exception:
            logger.exception("Initial Supabase sync")
    else:
        logger.warning("No valid auth token — Supabase sync skipped")
        if auth.has_credentials():
            logger.warning(
                "Could not refresh auth token — operating offline with cached SQLite data",
            )

    stop_event = threading.Event()

    def _supabase_pull_loop():
        while True:
            if stop_event.wait(SUPABASE_PULL_INTERVAL_SEC):
                return
            try:
                if auth.get_valid_token():
                    supabase_sync.pull_matters()
                    supabase_sync.pull_clients()
                    supabase_sync.pull_profile()
                    supabase_sync.pull_billing_status()
            except Exception:
                logger.exception("supabase pull loop")

    threading.Thread(
        target=_supabase_pull_loop,
        name="supabase-pull-loop",
        daemon=True,
    ).start()

    workers = [
        ("window-watcher",   window.run),
        ("afk-watcher",      afk.run),
        ("fs-watcher",       filesystem.run),
        ("classify-loop",    classify.run),
    ]

    threads: list[threading.Thread] = []
    for name, fn in workers:
        t = threading.Thread(
            target=_resilient_target,
            args=(name, fn, stop_event),
            name=name,
            daemon=True,
        )
        t.start()
        threads.append(t)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def _signal_handler(signum, frame):
        logger.info("Received signal %s — shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
        stop_event.set()

    for t in threads:
        t.join(timeout=5)

    lock.release()
    logger.info("ArdenTrack stopped")


if __name__ == "__main__":
    main()
