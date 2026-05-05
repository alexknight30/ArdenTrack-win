"""
ArdenTrack — Centralized path resolution.

DATA_DIR  (%LOCALAPPDATA%/Arden) holds high-I/O files that must NOT
live on OneDrive: the SQLite database, log file, and lock file.

USERDATA_DIR  (%LOCALAPPDATA%/Arden/userdata) holds config files shared
with the Arden Electron app: profile.json, matters.json, clients.json,
user_context.json, sheet.csv.  Kept inside DATA_DIR so auto-updates
(which replace the install directory) never overwrite user data.
"""

import os
import shutil
import sys


def _get_base_dir():
    """Return the directory containing the running executable (or script in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(os.path.join(__file__, os.pardir)))


BASE_DIR = _get_base_dir()

DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Arden",
)
os.makedirs(DATA_DIR, exist_ok=True)

USERDATA_DIR = os.path.join(DATA_DIR, "userdata")
os.makedirs(USERDATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "ardentrack.db")
LOG_PATH = os.path.join(DATA_DIR, "ardentrack.log")
LOCK_PATH = os.path.join(DATA_DIR, "ardentrack.lock")


def _migrate_userdata_from_install_dir():
    """One-time migration: copy files from the old exe-relative userdata into
    the new LOCALAPPDATA location so existing installs don't lose data on their
    first update."""
    old_dir = os.path.join(BASE_DIR, "userdata")
    if not os.path.isdir(old_dir) or os.path.normcase(os.path.abspath(old_dir)) == os.path.normcase(os.path.abspath(USERDATA_DIR)):
        return
    for fname in os.listdir(old_dir):
        src = os.path.join(old_dir, fname)
        dst = os.path.join(USERDATA_DIR, fname)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)


_migrate_userdata_from_install_dir()
