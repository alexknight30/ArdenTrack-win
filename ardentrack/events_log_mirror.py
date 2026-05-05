"""
Mirror SQLite `events` rows to userdata/log.csv for dev inspection.

Same CSV shape as utils/events_log_export (stdlib csv, comma-delimited).
Disabled when ARDEN_MIRROR_EVENTS_LOG is 0/false/off.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from filelock import FileLock

from ardentrack.paths import USERDATA_DIR

logger = logging.getLogger(__name__)

LOG_FILENAME = "log.csv"


def _mirror_enabled() -> bool:
    raw = os.environ.get("ARDEN_MIRROR_EVENTS_LOG", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_event_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def sync_events_log_csv(
    conn: Any | None = None,
    limit: int = 50000,
    chronological: bool = True,
    since_minutes: int | None = 30,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """
    Write SQLite `events` to USERDATA_DIR/log.csv.

    If *conn* is None, uses ardentrack.db (drains write queue first so inserts are visible).

    *force*: if True, write even when ARDEN_MIRROR_EVENTS_LOG is off (for explicit API/export).

    Env:
      ARDEN_MIRROR_EVENTS_LOG — default on; set 0 to skip automatic mirrors only.
      ARDEN_EVENTS_LOG_SINCE_MINUTES — default 30; set empty/0 for full table up to limit.
      ARDEN_EVENTS_LOG_LIMIT — max rows (default 50000, capped at 200000).
    """
    if not force and not _mirror_enabled():
        return {
            "ok": False,
            "skipped": True,
            "reason": "ARDEN_MIRROR_EVENTS_LOG disabled",
        }

    since_env = os.environ.get("ARDEN_EVENTS_LOG_SINCE_MINUTES", "").strip()
    if since_env:
        try:
            sm = int(since_env)
            since_minutes = None if sm <= 0 else sm
        except ValueError:
            pass

    limit = max(1, min(_env_int("ARDEN_EVENTS_LOG_LIMIT", limit), 200_000))
    order = "ASC" if chronological else "DESC"
    log_path = os.path.join(USERDATA_DIR, LOG_FILENAME)
    lock_path = log_path + ".lock"

    if conn is None:
        from ardentrack import db as at_db

        at_db._flush_writes()
        conn = at_db.get_db_connection()

    try:
        if since_minutes is not None:
            window = max(1, min(int(since_minutes), 24 * 60))
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)
            cur = conn.execute("SELECT * FROM events ORDER BY timestamp DESC")
            colnames = [d[0] for d in cur.description] if cur.description else []
            picked: list[Any] = []
            for row in cur:
                t = _parse_event_timestamp(row["timestamp"])
                if t is None:
                    continue
                if t < cutoff:
                    break
                picked.append(row)
                if len(picked) >= limit:
                    break
            if chronological:
                picked.reverse()
            rows = picked
        else:
            cur = conn.execute(
                f"SELECT * FROM events ORDER BY timestamp {order} LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            colnames = [d[0] for d in cur.description] if cur.description else []
    except Exception:
        logger.exception("sync_events_log_csv: query failed")
        raise

    os.makedirs(USERDATA_DIR, exist_ok=True)
    header = colnames if colnames else []

    def _cell(row: Any, col: str) -> str | int | float:
        try:
            v = row[col]
        except (KeyError, IndexError, TypeError):
            return ""
        if v is None:
            return ""
        return v

    try:
        lock = FileLock(lock_path, timeout=10)
        with lock:
            with open(log_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                for row in rows:
                    w.writerow([_cell(row, c) for c in header])
    except Exception:
        logger.exception("sync_events_log_csv: write failed")
        raise

    return {
        "ok": True,
        "path": os.path.abspath(log_path),
        "rows_written": len(rows),
        "columns": header,
        "order": "timestamp " + order if since_minutes is None else "newest-first scan, then "
        + ("asc" if chronological else "desc"),
        "limit": limit,
        "since_minutes": since_minutes,
    }
