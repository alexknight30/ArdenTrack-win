"""
ArdenTrack — SQLite database setup and write helpers.

Manages ardentrack.db in %LOCALAPPDATA%/Arden (off OneDrive).
Uses WAL mode for safe concurrent reads from the Arden frontend.
Per-thread connection caching and a write queue reduce I/O overhead.

--- Shared access contract ---
Arden (Electron frontend) opens this DB read/write via WAL mode.
Tables Arden may WRITE to: time_entries, matters, clients, profile,
  billing_codes, auth_user, corrections, integrations, telemetry (app events)
ArdenTrack writes: events, chunks, classify_cycle rows in telemetry, device (bootstrap).
Arden should only READ: events, chunks, device (uuid); may INSERT app telemetry rows.
busy_timeout=5000 prevents SQLITE_BUSY under WAL contention.
"""

import json
import logging
import os
import sqlite3
import threading
import uuid as _uuid
from datetime import datetime, timedelta

from tzlocal import get_localzone

from ardentrack.paths import DB_PATH

logger = logging.getLogger(__name__)

_TZ = get_localzone()

# Per-thread connection cache
_local = threading.local()

# ---------------------------------------------------------------------------
# Write queue — batches INSERT+COMMIT to reduce fsync frequency
# ---------------------------------------------------------------------------

_write_queue: list = []
_queue_lock = threading.Lock()
_flush_timer: threading.Timer | None = None
_FLUSH_INTERVAL = 5  # seconds

# ---------------------------------------------------------------------------
# Migrations — PRAGMA user_version tracks which have run
# ---------------------------------------------------------------------------

def _migrate_v0_baseline(conn):
    """Original schema: events + chunks + indexes."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id                      TEXT PRIMARY KEY,
            source                  TEXT NOT NULL,
            timestamp               TEXT NOT NULL,
            duration_s              REAL,
            app_name                TEXT,
            window_title            TEXT,
            process_path            TEXT,
            document_path           TEXT,
            is_afk                  INTEGER,
            input_density           REAL,
            file_path               TEXT,
            file_event_type         TEXT,
            email_subject           TEXT,
            email_sender            TEXT,
            email_recipients        TEXT,
            email_direction         TEXT,
            calendar_title          TEXT,
            calendar_attendees      TEXT,
            call_platform           TEXT,
            call_participants       TEXT,
            call_topic              TEXT,
            matter_hint             TEXT,
            matter_hint_source      TEXT,
            matter_hint_confidence  REAL,
            external_id             TEXT,
            chunk_id                TEXT,
            classified              INTEGER DEFAULT 0,
            created_at              TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id              TEXT PRIMARY KEY,
            start_time      TEXT NOT NULL,
            end_time        TEXT NOT NULL,
            duration_s      REAL NOT NULL,
            event_ids       TEXT NOT NULL,
            matter_hint     TEXT,
            classified      INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_events_classified ON events(classified);
        CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
    """)


def _migrate_v1_data_layer(conn):
    """Add matters, clients, time_entries, profile, billing_codes, device, auth_user."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS matters (
            name                 TEXT PRIMARY KEY,
            shorthand            TEXT,
            client               TEXT,
            description          TEXT DEFAULT '',
            color                TEXT,
            rate                 REAL,
            billable             INTEGER DEFAULT 1,
            weekly_hours_goal    REAL,
            practice_area        TEXT,
            clio_matter_id       TEXT,
            external_sync_source TEXT,
            last_synced_at       TEXT,
            created_at           TEXT DEFAULT (datetime('now')),
            updated_at           TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS clients (
            name                    TEXT PRIMARY KEY,
            color                   TEXT,
            client_id               TEXT,
            contact_person_name     TEXT,
            email                   TEXT,
            phone                   TEXT,
            mailing_address         TEXT,
            client_type             TEXT DEFAULT '',
            client_status           TEXT DEFAULT '',
            notes                   TEXT,
            date_opened             TEXT,
            billing_contact_info    TEXT,
            associated_matters      TEXT,
            industry                TEXT,
            conflict_check_status   TEXT DEFAULT '',
            referral_source         TEXT,
            clio_client_id          TEXT,
            created_at              TEXT DEFAULT (datetime('now')),
            updated_at              TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS time_entries (
            id                  TEXT PRIMARY KEY,
            date                TEXT NOT NULL,
            start_time          TEXT NOT NULL,
            duration_min        REAL NOT NULL,
            description         TEXT DEFAULT '',
            matter              TEXT,
            confidence          REAL,
            reviewed            INTEGER DEFAULT 0,
            outstanding_flags   TEXT DEFAULT '',
            task_code           TEXT,
            activity_code       TEXT,
            chunk_id            TEXT,
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_time_entries_date ON time_entries(date);
        CREATE INDEX IF NOT EXISTS idx_time_entries_matter ON time_entries(matter);

        CREATE TABLE IF NOT EXISTS profile (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            firm_name           TEXT DEFAULT '',
            firm_address        TEXT DEFAULT '',
            firm_city           TEXT DEFAULT '',
            firm_state          TEXT DEFAULT '',
            firm_zip            TEXT DEFAULT '',
            firm_phone          TEXT DEFAULT '',
            firm_email          TEXT DEFAULT '',
            timekeeper_name     TEXT DEFAULT '',
            yearly_hour_goal    REAL DEFAULT 0,
            license_key         TEXT DEFAULT '',
            updated_at          TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS billing_codes (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            disabled_task_codes     TEXT DEFAULT '[]',
            disabled_activity_codes TEXT DEFAULT '[]',
            custom_task_codes       TEXT DEFAULT '[]',
            custom_activity_codes   TEXT DEFAULT '[]',
            updated_at              TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS device (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            uuid        TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS auth_user (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            user_id             TEXT,
            email               TEXT,
            name                TEXT,
            firm_name           TEXT,
            billing_status      TEXT,
            is_premium          INTEGER DEFAULT 0,
            authenticated_at    TEXT,
            updated_at          TEXT DEFAULT (datetime('now'))
        );
    """)

    # Auto-populate device UUID on first creation
    existing = conn.execute("SELECT uuid FROM device WHERE id = 1").fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO device (id, uuid) VALUES (1, ?)",
            (str(_uuid.uuid4()),),
        )


def _migrate_v2_corrections_telemetry(conn):
    """Add corrections and telemetry tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS corrections (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            original_matter     TEXT NOT NULL,
            corrected_matter    TEXT NOT NULL,
            app_name            TEXT,
            title_keywords      TEXT,
            count               INTEGER DEFAULT 1,
            last_corrected_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_corrections_matters
            ON corrections(original_matter, corrected_matter);

        CREATE TABLE IF NOT EXISTS telemetry (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT DEFAULT (datetime('now')),
            event_type          TEXT NOT NULL,
            events_collected    INTEGER,
            chunks_classified   INTEGER,
            uptime_min          REAL,
            metadata            TEXT,
            synced              INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(timestamp);
    """)


def _migrate_v3_integrations(conn):
    """OAuth tokens for external integrations (e.g. Clio)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS integrations (
            provider        TEXT PRIMARY KEY,
            access_token    TEXT,
            refresh_token   TEXT,
            expires_at      TEXT,
            scope           TEXT,
            metadata        TEXT,
            updated_at      TEXT DEFAULT (datetime('now'))
        );
    """)


def _migrate_v4_event_slot_completion(conn):
    """
    Per-(event, 6-minute slot) classification progress.

    Overlapping chunks can classify the same event in multiple slots; we mark
    events.classified only after every required slot has succeeded at least once.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_slot_completion (
            event_id    TEXT NOT NULL,
            slot_start  TEXT NOT NULL,
            done        INTEGER NOT NULL DEFAULT 1,
            chunk_id    TEXT,
            updated_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (event_id, slot_start)
        );
        CREATE INDEX IF NOT EXISTS idx_event_slot_completion_event
            ON event_slot_completion(event_id);
    """)


def _migrate_v5_clio_synced_codes(conn):
    """Persist activity/task codes synced from Clio inside billing_codes."""
    for col, default in [
        ("clio_synced_activity_codes", "'[]'"),
        ("clio_synced_task_codes", "'[]'"),
        ("clio_last_synced", "NULL"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE billing_codes ADD COLUMN {col} TEXT DEFAULT {default}"
            )
        except Exception:
            pass


def _migrate_v6_cloud_sync(conn):
    """Track which time_entries have been pushed to Supabase."""
    try:
        conn.execute(
            "ALTER TABLE time_entries ADD COLUMN synced_to_cloud INTEGER DEFAULT 0"
        )
    except Exception:
        pass


_MIGRATIONS = [
    _migrate_v0_baseline,
    _migrate_v1_data_layer,
    _migrate_v2_corrections_telemetry,
    _migrate_v3_integrations,
    _migrate_v4_event_slot_completion,
    _migrate_v5_clio_synced_codes,
    _migrate_v6_cloud_sync,
]

# ---------------------------------------------------------------------------
# Column lists (keep in sync with schema)
# ---------------------------------------------------------------------------

EVENT_COLUMNS = [
    "id", "source", "timestamp", "duration_s",
    "app_name", "window_title", "process_path", "document_path",
    "is_afk", "input_density",
    "file_path", "file_event_type",
    "email_subject", "email_sender", "email_recipients", "email_direction",
    "calendar_title", "calendar_attendees",
    "call_platform", "call_participants", "call_topic",
    "matter_hint", "matter_hint_source", "matter_hint_confidence",
    "external_id", "chunk_id", "classified", "created_at",
]

CHUNK_COLUMNS = [
    "id", "start_time", "end_time", "duration_s",
    "event_ids", "matter_hint", "classified", "created_at",
]

TIME_ENTRY_COLUMNS = [
    "id", "date", "start_time", "duration_min", "description",
    "matter", "confidence", "reviewed", "outstanding_flags",
    "task_code", "activity_code", "chunk_id",
    "created_at", "updated_at", "synced_to_cloud",
]

MATTER_COLUMNS = [
    "name", "shorthand", "client", "description", "color", "rate",
    "billable", "weekly_hours_goal", "practice_area",
    "clio_matter_id", "external_sync_source", "last_synced_at",
    "created_at", "updated_at",
]

CLIENT_COLUMNS = [
    "name", "color", "client_id", "contact_person_name", "email",
    "phone", "mailing_address", "client_type", "client_status",
    "notes", "date_opened", "billing_contact_info",
    "associated_matters", "industry", "conflict_check_status",
    "referral_source", "clio_client_id", "created_at", "updated_at",
]

PROFILE_COLUMNS = [
    "id", "firm_name", "firm_address", "firm_city", "firm_state", "firm_zip",
    "firm_phone", "firm_email", "timekeeper_name", "yearly_hour_goal",
    "license_key", "updated_at",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(tz=_TZ).isoformat()


# Namespace for deterministic time_entry ids (dedupe same date+start_time slot).
_TIME_ENTRY_SLOT_NS = _uuid.UUID("a1b2c3d4-e5f6-4780-abcd-ef1020304050")


def time_entry_id_for_slot(date: str, start_time: str) -> str:
    """Stable primary key for a clock slot so INSERT OR REPLACE updates one row."""
    return str(_uuid.uuid5(_TIME_ENTRY_SLOT_NS, f"{date}|{start_time}"))


def get_db_connection():
    """
    Return a cached per-thread SQLite connection with WAL mode enabled.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _local.conn = conn
    return conn


def _try_mirror_events_log(conn):
    """Refresh userdata/log.csv from `events` after mutations (dev; no-op if disabled)."""
    try:
        from ardentrack.events_log_mirror import sync_events_log_csv

        sync_events_log_csv(conn=conn)
    except Exception:
        logger.warning("Could not mirror events to log.csv", exc_info=True)


def _flush_writes():
    """Drain the write queue into the database with a single commit."""
    with _queue_lock:
        if not _write_queue:
            return
        batch = list(_write_queue)
        _write_queue.clear()

    conn = get_db_connection()
    for sql, values in batch:
        conn.execute(sql, values)
    conn.commit()
    _try_mirror_events_log(conn)


def _flush_timer_loop():
    """Periodic flush on a daemon timer."""
    global _flush_timer
    try:
        _flush_writes()
    except Exception:
        logger.exception("Error flushing write queue")
    _flush_timer = threading.Timer(_FLUSH_INTERVAL, _flush_timer_loop)
    _flush_timer.daemon = True
    _flush_timer.start()


def setup():
    """Run migrations and start the flush timer."""
    global _flush_timer
    logger.info("Setting up database at %s", DB_PATH)
    conn = get_db_connection()

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    target_version = len(_MIGRATIONS) - 1

    if current_version < target_version:
        for i, fn in enumerate(_MIGRATIONS):
            if i > current_version:
                logger.info("Running migration v%d", i)
                fn(conn)
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.commit()
        logger.info("Database migrated to v%d", target_version)
    elif current_version == 0 and target_version == 0:
        _migrate_v0_baseline(conn)
        conn.commit()
    else:
        logger.info("Database already at v%d", current_version)

    # Idempotent: ensure baseline tables exist even on a fresh DB
    _migrate_v0_baseline(conn)
    conn.commit()

    _flush_timer = threading.Timer(_FLUSH_INTERVAL, _flush_timer_loop)
    _flush_timer.daemon = True
    _flush_timer.start()


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def insert_event(event_dict: dict):
    """
    Queue a single event row for batched insertion.

    The caller can omit 'id' and 'created_at' — they are auto-generated.
    """
    event = dict(event_dict)
    if "id" not in event or not event["id"]:
        event["id"] = str(_uuid.uuid4())
    if "created_at" not in event or not event["created_at"]:
        event["created_at"] = _now_iso()

    cols = [c for c in EVENT_COLUMNS if c in event]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [event[c] for c in cols]

    sql = f"INSERT OR REPLACE INTO events ({col_names}) VALUES ({placeholders})"

    with _queue_lock:
        _write_queue.append((sql, values))

    return event["id"]


def get_events_by_source(source: str, start_iso: str, end_iso: str) -> list:
    """Return events of the given source between start_iso and end_iso (inclusive)."""
    _flush_writes()
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM events WHERE source = ? AND timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp ASC",
        (source, start_iso, end_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unclassified_events(older_than_minutes: int = 2):
    _flush_writes()

    cutoff = datetime.now(tz=_TZ) - timedelta(minutes=older_than_minutes)
    cutoff_iso = cutoff.isoformat()

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM events "
        "WHERE classified = 0 AND source = 'window' AND timestamp < ? "
        "ORDER BY timestamp ASC",
        (cutoff_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_events_classified(event_ids: list, chunk_id: str):
    if not event_ids:
        return
    conn = get_db_connection()
    placeholders = ", ".join(["?"] * len(event_ids))
    conn.execute(
        f"UPDATE events SET classified = 1, chunk_id = ? "
        f"WHERE id IN ({placeholders})",
        [chunk_id] + list(event_ids),
    )
    conn.commit()
    _try_mirror_events_log(conn)


def upsert_event_slot_done(event_id: str, slot_start: str, chunk_id: str | None = None) -> None:
    """Record that a successful classify covered this event in this 6-minute wall slot."""
    now = _now_iso()
    conn = get_db_connection()
    conn.execute(
        """
        INSERT OR REPLACE INTO event_slot_completion
            (event_id, slot_start, done, chunk_id, updated_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (event_id, slot_start, chunk_id, now),
    )
    conn.commit()


def event_has_all_required_slots_done(event_id: str, required_slot_starts: list) -> bool:
    """True when every required slot has a completed row for this event."""
    if not required_slot_starts:
        return False
    conn = get_db_connection()
    placeholders = ",".join(["?"] * len(required_slot_starts))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM event_slot_completion "
        f"WHERE event_id = ? AND done = 1 AND slot_start IN ({placeholders})",
        [event_id] + list(required_slot_starts),
    ).fetchone()
    n = row["n"] if row is not None else 0
    return n >= len(required_slot_starts)


def mark_single_event_classified(event_id: str, chunk_id: str) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE events SET classified = 1, chunk_id = ? WHERE id = ?",
        (chunk_id, event_id),
    )
    conn.commit()
    _try_mirror_events_log(conn)


def insert_chunk(chunk_dict: dict):
    """Insert a chunk row. event_ids is stored as a JSON array string."""
    chunk = dict(chunk_dict)
    if "id" not in chunk or not chunk["id"]:
        chunk["id"] = str(_uuid.uuid4())
    if "created_at" not in chunk or not chunk["created_at"]:
        chunk["created_at"] = _now_iso()

    if isinstance(chunk.get("event_ids"), list):
        chunk["event_ids"] = json.dumps(chunk["event_ids"])

    cols = [c for c in CHUNK_COLUMNS if c in chunk]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [chunk[c] for c in cols]

    conn = get_db_connection()
    conn.execute(
        f"INSERT OR REPLACE INTO chunks ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return chunk["id"]


def update_event(event_id: str, updates: dict):
    if not updates:
        return
    _flush_writes()

    set_clause = ", ".join([f"{k} = ?" for k in updates])
    values = list(updates.values()) + [event_id]

    conn = get_db_connection()
    conn.execute(
        f"UPDATE events SET {set_clause} WHERE id = ?",
        values,
    )
    conn.commit()
    _try_mirror_events_log(conn)


def delete_events(event_ids: list):
    if not event_ids:
        return
    _flush_writes()

    placeholders = ", ".join(["?"] * len(event_ids))
    conn = get_db_connection()
    conn.execute(
        f"DELETE FROM events WHERE id IN ({placeholders})",
        event_ids,
    )
    conn.commit()
    _try_mirror_events_log(conn)


def get_recent_window_events(minutes: int = 15):
    _flush_writes()

    cutoff = datetime.now(tz=_TZ) - timedelta(minutes=minutes)
    cutoff_iso = cutoff.isoformat()

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM events "
        "WHERE source = 'window' AND timestamp > ? "
        "ORDER BY timestamp ASC",
        (cutoff_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Time entry helpers
# ---------------------------------------------------------------------------

def delete_time_entry_by_slot(date: str, start_time: str) -> None:
    """Remove a time entry by stable slot id (used when merging consecutive chunks)."""
    eid = time_entry_id_for_slot(str(date), str(start_time))
    conn = get_db_connection()
    conn.execute("DELETE FROM time_entries WHERE id = ?", (eid,))
    conn.commit()


def time_entry_exists_for_slot(date: str, start_time: str) -> bool:
    """True if a time_entry already covers this 6-minute wall slot."""
    eid = time_entry_id_for_slot(str(date), str(start_time))
    conn = get_db_connection()
    row = conn.execute("SELECT 1 FROM time_entries WHERE id = ?", (eid,)).fetchone()
    return row is not None


def insert_time_entry(entry_dict: dict):
    """Insert or replace a time entry row."""
    entry = dict(entry_dict)
    if "synced_to_cloud" not in entry:
        entry["synced_to_cloud"] = 0
    if "id" not in entry or not entry["id"]:
        d = entry.get("date")
        st = entry.get("start_time")
        if d and st:
            entry["id"] = time_entry_id_for_slot(str(d), str(st))
        else:
            entry["id"] = str(_uuid.uuid4())
    if "created_at" not in entry or not entry["created_at"]:
        entry["created_at"] = _now_iso()
    if "updated_at" not in entry or not entry["updated_at"]:
        entry["updated_at"] = _now_iso()

    cols = [c for c in TIME_ENTRY_COLUMNS if c in entry]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [entry[c] for c in cols]

    conn = get_db_connection()
    conn.execute(
        f"INSERT OR REPLACE INTO time_entries ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return entry["id"]


def get_recent_time_entries(n: int = 20) -> list:
    """Return the last *n* time entries ordered by date+start_time descending."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM time_entries "
        "ORDER BY date DESC, start_time DESC "
        "LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def sync_sheet_csv_from_db():
    """
    Rewrite userdata/sheet.csv from time_entries (same columns as api/sheet._sync_csv_from_db).
    Call after classify or whenever the DB is the source of truth for entries.
    """
    import csv

    from filelock import FileLock

    from ardentrack.paths import USERDATA_DIR

    sheet_path = os.path.join(USERDATA_DIR, "sheet.csv")
    lock_path = sheet_path + ".lock"
    _flush_writes()
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT date, start_time, duration_min, description, matter, confidence, "
        "reviewed, outstanding_flags, task_code, activity_code "
        "FROM time_entries ORDER BY date ASC, start_time ASC"
    ).fetchall()
    os.makedirs(os.path.dirname(sheet_path) or ".", exist_ok=True)
    lock = FileLock(lock_path, timeout=10)
    with lock:
        with open(sheet_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="|")
            w.writerow(
                [
                    "date",
                    "start_time",
                    "duration",
                    "description",
                    "matter",
                    "confidence",
                    "reviewed",
                    "outstandingFlags",
                    "task_code",
                    "activity_code",
                ]
            )
            for r in rows:
                w.writerow(
                    [
                        r["date"],
                        r["start_time"],
                        r["duration_min"],
                        r["description"],
                        r["matter"],
                        r["confidence"] if r["confidence"] is not None else "",
                        "yes" if r["reviewed"] else "no",
                        r["outstanding_flags"] or "",
                        r["task_code"] or "",
                        r["activity_code"] or "",
                    ]
                )


# ---------------------------------------------------------------------------
# Matter / client helpers
# ---------------------------------------------------------------------------

def get_all_matters() -> list:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM matters ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_all_clients() -> list:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def upsert_matter(matter_dict: dict):
    """Insert or replace a matter row."""
    matter = dict(matter_dict)
    if "updated_at" not in matter:
        matter["updated_at"] = _now_iso()
    if "created_at" not in matter:
        matter["created_at"] = _now_iso()

    cols = [c for c in MATTER_COLUMNS if c in matter]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [matter[c] for c in cols]

    conn = get_db_connection()
    conn.execute(
        f"INSERT OR REPLACE INTO matters ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def upsert_client(client_dict: dict):
    """Insert or replace a client row."""
    client = dict(client_dict)
    if "updated_at" not in client:
        client["updated_at"] = _now_iso()
    if "created_at" not in client:
        client["created_at"] = _now_iso()

    cols = [c for c in CLIENT_COLUMNS if c in client]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [client[c] for c in cols]

    conn = get_db_connection()
    conn.execute(
        f"INSERT OR REPLACE INTO clients ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def get_matter_by_name(name: str) -> dict | None:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM matters WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def get_client_by_name(name: str) -> dict | None:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM clients WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def get_profile_row() -> dict | None:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    return dict(row) if row else None


def upsert_profile(profile_dict: dict):
    """Insert or replace the single profile row (id = 1)."""
    profile = dict(profile_dict)
    profile["id"] = 1
    if "updated_at" not in profile:
        profile["updated_at"] = _now_iso()

    cols = [c for c in PROFILE_COLUMNS if c in profile]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [profile[c] for c in cols]

    conn = get_db_connection()
    conn.execute(
        f"INSERT OR REPLACE INTO profile ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def upsert_auth_user(
    user_id: str | None,
    email: str | None,
    name: str | None,
    firm_name: str | None,
    billing_status: str | None,
    is_premium: int,
) -> None:
    conn = get_db_connection()
    conn.execute(
        """
        INSERT OR REPLACE INTO auth_user
        (id, user_id, email, name, firm_name, billing_status, is_premium,
         authenticated_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (user_id, email, name, firm_name, billing_status, is_premium),
    )
    conn.commit()


def get_auth_user_row() -> dict | None:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM auth_user WHERE id = 1").fetchone()
    return dict(row) if row else None


def get_unsynced_entries() -> list:
    """Return time_entries not yet successfully pushed to Supabase."""
    _flush_writes()
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM time_entries WHERE synced_to_cloud = 0 "
        "ORDER BY date ASC, start_time ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_entry_synced(entry_id: str) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE time_entries SET synced_to_cloud = 1 WHERE id = ?",
        (entry_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Correction helpers
# ---------------------------------------------------------------------------

def get_recent_corrections(limit: int = 50) -> list:
    """Return corrections ordered by frequency then recency."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM corrections "
        "ORDER BY count DESC, last_corrected_at DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_correction(original_matter: str, corrected_matter: str,
                      app_name: str | None = None,
                      title_keywords: str | None = None):
    """Record a user correction, incrementing count if it already exists."""
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id, count FROM corrections "
        "WHERE original_matter = ? AND corrected_matter = ? AND "
        "COALESCE(app_name, '') = COALESCE(?, '')",
        (original_matter, corrected_matter, app_name),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE corrections SET count = count + 1, "
            "last_corrected_at = datetime('now'), "
            "title_keywords = COALESCE(?, title_keywords) "
            "WHERE id = ?",
            (title_keywords, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO corrections "
            "(original_matter, corrected_matter, app_name, title_keywords) "
            "VALUES (?, ?, ?, ?)",
            (original_matter, corrected_matter, app_name, title_keywords),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def insert_telemetry(event_type: str, events_collected: int | None = None,
                     chunks_classified: int | None = None,
                     uptime_min: float | None = None,
                     metadata: str | None = None):
    """Insert a telemetry row."""
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO telemetry "
        "(event_type, events_collected, chunks_classified, uptime_min, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, events_collected, chunks_classified, uptime_min, metadata),
    )
    conn.commit()


def get_unsynced_telemetry(limit: int = 500) -> list:
    """Return telemetry rows not yet pushed to Supabase."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM telemetry WHERE synced = 0 "
        "ORDER BY timestamp ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_telemetry_synced(ids: list):
    """Mark telemetry rows as synced after a successful Supabase push."""
    if not ids:
        return
    conn = get_db_connection()
    placeholders = ", ".join(["?"] * len(ids))
    conn.execute(
        f"UPDATE telemetry SET synced = 1 WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()


def is_user_subscribed() -> bool:
    """Return True if the auth_user row shows an active or trialing subscription."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT billing_status FROM auth_user WHERE id = 1"
    ).fetchone()
    if not row:
        return False
    status = (row["billing_status"] or "").strip().lower()
    return status in ("active", "trialing")
