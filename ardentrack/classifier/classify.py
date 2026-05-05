"""
ArdenTrack — Classification loop.

Runs on a periodic interval (default 120s, env ARDEN_CLASSIFY_INTERVAL_SEC).  Reads unclassified events from the database,
chunks them, posts each chunk to the remote classifier API, and writes
classified time entries to the time_entries table (and sheet.csv as a
compatibility shim).

userdata/log.csv is NOT read here — it is mirrored from SQLite `events` by
`events_log_mirror` after writes (and by Flask export). Classify input comes from the DB only.
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from tzlocal import get_localzone

from ardentrack import db
from ardentrack.paths import USERDATA_DIR
from ardentrack.classifier import smoother, chunker

logger = logging.getLogger(__name__)

logging.getLogger("urllib3").setLevel(logging.WARNING)

_TZ = get_localzone()


def _notify_flask_sheet_changed() -> None:
    """Tell local Flask to enqueue a sheet refresh for open UIs (best-effort; no-op if Flask is down)."""
    port = (os.environ.get("PORT") or "5000").strip() or "5000"
    url = f"http://127.0.0.1:{port}/api/notify-sheet-updated"
    headers = {}
    token = os.environ.get("ARDEN_NOTIFY_TOKEN", "").strip()
    if token:
        headers["X-Arden-Notify-Token"] = token
    try:
        requests.post(url, timeout=2, headers=headers)
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Poll interval (seconds). Chunks are still 6-minute clock windows; this only controls how
# often we look for newly-settled events. Override with ARDEN_CLASSIFY_INTERVAL_SEC.
CLASSIFY_INTERVAL = _env_int("ARDEN_CLASSIFY_INTERVAL_SEC", 120)
API_URL = "https://as-api.onrender.com/api/classify"
API_TIMEOUT = 60          # seconds
MAX_CONSECUTIVE_FAILURES = 3

_START_TIME = time.monotonic()


def _get_uptime_min() -> float:
    return round((time.monotonic() - _START_TIME) / 60, 2)


SHEET_COLUMNS = [
    "date", "start_time", "duration", "description", "matter",
    "confidence", "reviewed", "outstandingFlags", "task_code", "activity_code",
]


def _userdata_path(*parts):
    return os.path.join(USERDATA_DIR, *parts)


def _is_quiet_hours() -> bool:
    return False


def _load_json_file(filename: str):
    path = _userdata_path(filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load %s: %s", filename, exc)
        return None


def _load_recent_work(n: int = 20) -> list:
    """
    Load recent classified work for API context.  The Render API expects
    each entry as ``{"time": "YYYY-MM-DD HH:MM", "matter": str, "confidence": float}``.
    Prefers the time_entries DB table; falls back to sheet.csv.
    Does not read userdata/log.csv (that file is unrelated to classification).
    """
    entries = db.get_recent_time_entries(n=n)
    if entries:
        results = []
        for e in entries:
            try:
                conf = float(e.get("confidence", 0.5) or 0.5)
            except (ValueError, TypeError):
                conf = 0.5
            results.append({
                "time": f"{e.get('date', '')} {e.get('start_time', '')}",
                "matter": e.get("matter", ""),
                "confidence": conf,
            })
        results.sort(key=lambda x: x["time"])
        return results

    # Fallback: read from sheet.csv
    path = _userdata_path("sheet.csv")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if not lines:
            return []

        data_lines = [l for l in lines if l.strip()]
        recent = data_lines[-n:]

        results = []
        for line in recent:
            parts = line.strip().split("|")
            if len(parts) >= 5:
                row = dict(zip(SHEET_COLUMNS, parts[: len(SHEET_COLUMNS)]))
                try:
                    conf = float(row.get("confidence", 0.5) or 0.5)
                except (ValueError, TypeError):
                    conf = 0.5
                results.append({
                    "time": f"{row.get('date', '')} {row.get('start_time', '')}",
                    "matter": row.get("matter", ""),
                    "confidence": conf,
                })
        results.sort(key=lambda x: x["time"])
        return results
    except OSError as exc:
        logger.warning("Could not read sheet.csv: %s", exc)
        return []


def _load_matters() -> dict:
    """Load matters as a dict keyed by name (the format the Render API expects)."""
    db_matters = db.get_all_matters()
    if db_matters:
        return {m["name"]: m for m in db_matters}
    raw = _load_json_file("matters.json")
    if isinstance(raw, dict):
        return raw
    return {}


def _load_clients() -> dict:
    """Load clients as a dict keyed by name (the format the Render API expects)."""
    db_clients = db.get_all_clients()
    if db_clients:
        return {c["name"]: c for c in db_clients}
    raw = _load_json_file("clients.json")
    if isinstance(raw, dict):
        return raw
    return {}


def _load_corrections() -> str:
    """Load correction history as a formatted string for the Render API."""
    corrections = db.get_recent_corrections(limit=50)
    if not corrections:
        return ""

    lines = []
    for c in corrections:
        line = f'- "{c["original_matter"]}" was reassigned to "{c["corrected_matter"]}"'
        if c.get("app_name"):
            line += f' when using {c["app_name"]}'
        line += f' ({c["count"]} time{"s" if c["count"] != 1 else ""})'
        lines.append(line)

    return "CORRECTION HISTORY (user has corrected these before):\n" + "\n".join(lines)


def _build_payload(chunk: dict, matters, clients) -> dict:
    slot_start = _parse_ts_iso(chunk["start_time"])
    slot_end = _parse_ts_iso(chunk["end_time"])
    events_payload = []
    by_app = defaultdict(float)
    for ev in chunk.get("events", []):
        w = _overlap_weight_seconds(ev, slot_start, slot_end)
        if w <= 0:
            continue
        app = (ev.get("app_name") or "").strip() or "(unknown)"
        by_app[app] += w
        events_payload.append({
            "app": ev.get("app_name", ""),
            "title": ev.get("window_title", ""),
            "duration_seconds": w,
        })
    if not events_payload and chunk.get("events"):
        logger.warning(
            "Chunk %s: zero overlap weight for events; falling back to raw durations",
            chunk.get("id", "")[:8],
        )
        for ev in chunk["events"]:
            dur = float(ev.get("duration_s") or 0)
            app = (ev.get("app_name") or "").strip() or "(unknown)"
            by_app[app] += dur
            events_payload.append({
                "app": ev.get("app_name", ""),
                "title": ev.get("window_title", ""),
                "duration_seconds": dur,
            })

    ranked_apps = sorted(by_app.items(), key=lambda x: -x[1])[:20]

    payload = {
        "events": events_payload,
        "matters": matters or {},
        "clients": clients or {},
        "duration_seconds_by_app": [
            {"app": a, "seconds": round(s, 1)} for a, s in ranked_apps
        ],
    }
    return payload


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _parse_ts_iso(iso_str: str) -> datetime:
    s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return dt


def _overlap_weight_seconds(ev: dict, slot_start: datetime, slot_end: datetime) -> float:
    """Seconds of this event's interval that fall inside [slot_start, slot_end)."""
    ts = _parse_ts_iso(ev["timestamp"])
    try:
        dur = float(ev.get("duration_s") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    ev_end = ts + timedelta(seconds=dur)
    lo = max(ts, slot_start)
    hi = min(ev_end, slot_end)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds()


def _next_time_slot(start_time: str) -> str:
    """HH:MM + 6 minutes."""
    h, m = start_time.split(":")
    total = int(h) * 60 + int(m) + 6
    return f"{total // 60:02d}:{total % 60:02d}"


def _chunk_looks_like_video_call(chunk: dict) -> bool:
    for ev in chunk.get("events") or []:
        a = (ev.get("app_name") or "").lower()
        if "zoom" in a or "teams" in a or "webex" in a:
            return True
    return False


def _can_merge_video_slots(prev_item: dict, next_item: dict) -> bool:
    ra = prev_item["row"]
    rb = next_item["row"]
    if ra.get("date") != rb.get("date"):
        return False
    if ra.get("matter") != rb.get("matter"):
        return False
    if _next_time_slot(ra["start_time"]) != rb["start_time"]:
        return False
    return _chunk_looks_like_video_call(prev_item["chunk"]) and _chunk_looks_like_video_call(
        next_item["chunk"]
    )


def _merge_contiguous_video_slots(classified_rows: list) -> None:
    """Collapse consecutive 6-minute video-call rows with the same matter into one entry."""
    if len(classified_rows) < 2 or not _env_bool("ARDEN_MERGE_VIDEO_CHUNKS", True):
        return

    ordered = sorted(
        classified_rows,
        key=lambda x: (x["row"]["date"], x["row"]["start_time"]),
    )
    groups: list[list] = []
    i = 0
    while i < len(ordered):
        g = [ordered[i]]
        j = i + 1
        while j < len(ordered) and _can_merge_video_slots(g[-1], ordered[j]):
            g.append(ordered[j])
            j += 1
        groups.append(g)
        i = j

    for g in groups:
        if len(g) < 2:
            continue
        first = g[0]["row"]
        date = first["date"]
        start0 = first["start_time"]
        try:
            total_min = sum(float(x["row"]["duration"]) for x in g)
        except (TypeError, ValueError):
            total_min = 6.0 * len(g)
        confs = []
        for x in g:
            try:
                confs.append(float(x["row"]["confidence"]))
            except (TypeError, ValueError):
                confs.append(0.0)
        merged_conf = max(confs) if confs else 0.0
        desc = first.get("description", "")

        for x in g[1:]:
            st = x["row"]["start_time"]
            db.delete_time_entry_by_slot(date, st)

        eid = db.insert_time_entry({
            "date": date,
            "start_time": start0,
            "duration_min": total_min,
            "description": desc,
            "matter": first.get("matter", ""),
            "confidence": merged_conf,
            "reviewed": 0,
            "outstanding_flags": (first.get("outstandingFlags") or "").strip() or "",
            "task_code": first.get("task_code", ""),
            "activity_code": first.get("activity_code", ""),
            "chunk_id": g[0]["chunk"]["id"],
        })
        try:
            from ardentrack import supabase_sync

            supabase_sync.push_time_entry(
                {
                    "id": eid,
                    "date": date,
                    "start_time": start0,
                    "duration_min": total_min,
                    "description": desc,
                    "matter": first.get("matter", ""),
                    "confidence": merged_conf,
                    "reviewed": 0,
                    "outstanding_flags": (first.get("outstandingFlags") or "").strip() or "",
                    "task_code": first.get("task_code", ""),
                    "activity_code": first.get("activity_code", ""),
                    "chunk_id": g[0]["chunk"]["id"],
                }
            )
        except Exception:
            pass
        logger.info(
            "Merged %d consecutive video slots into %s %s (%.2f min)",
            len(g), date, start0, total_min,
        )


def _flag_low_confidence(classified_rows: list):
    """Mark bottom ~20% of batch plus any row at or below absolute low-confidence threshold."""
    low_abs = _env_float("ARDEN_LOW_CONF_FLAG_THRESHOLD", 0.65)

    if len(classified_rows) < 2:
        for row in classified_rows:
            try:
                conf = float(row.get("confidence", 0))
            except (ValueError, TypeError):
                conf = 0.0
            if conf <= low_abs:
                row["outstandingFlags"] = "yes"
        return

    rows_with_conf = []
    for row in classified_rows:
        try:
            conf = float(row.get("confidence", 0))
        except (ValueError, TypeError):
            conf = 0.0
        rows_with_conf.append((conf, row))

    rows_with_conf.sort(key=lambda x: x[0])
    cutoff_idx = max(1, len(rows_with_conf) // 5)
    threshold = rows_with_conf[cutoff_idx - 1][0]

    for conf, row in rows_with_conf:
        if conf <= threshold or conf <= low_abs:
            row["outstandingFlags"] = "yes"


def _apply_slot_completion_for_chunk(chunk: dict) -> None:
    """Record per-slot success and mark the event classified when all required slots are done."""
    ev_by_id = {e["id"]: e for e in (chunk.get("events") or []) if e.get("id")}
    now = datetime.now(tz=_TZ)
    for eid in chunk.get("event_ids") or []:
        ev = ev_by_id.get(eid)
        if not ev:
            logger.warning(
                "Chunk %s references event %s without a full events[] row — skipping slot completion",
                (chunk.get("id") or "")[:8],
                (eid or "")[:8],
            )
            continue
        db.upsert_event_slot_done(eid, chunk["start_time"], chunk["id"])
        required = chunker.required_slot_starts_for_event(ev)
        if not db.event_has_all_required_slots_done(eid, required):
            continue
        # Don't classify while the event's end time is close to "now" — the
        # smoother may still be extending its duration, which would add more
        # required slots.  Wait until the event is clearly in the past.
        ev_ts = _parse_ts_iso(ev["timestamp"])
        ev_dur = float(ev.get("duration_s") or 0)
        ev_end = ev_ts + timedelta(seconds=ev_dur)
        if ev_end >= now - timedelta(seconds=chunker.CHUNK_WINDOW):
            continue
        db.mark_single_event_classified(eid, chunk["id"])


def _classify_chunk(chunk: dict, matters, clients) -> dict | None:
    payload = _build_payload(chunk, matters, clients)

    try:
        headers = {}
        as_secret = (os.environ.get("ARDEN_AS_API_SECRET") or "").strip()
        if as_secret:
            headers["X-Arden-Secret"] = as_secret
        resp = requests.post(
            API_URL, json=payload, timeout=API_TIMEOUT, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            logger.warning("Classify API returned error for chunk %s: %s", chunk["id"][:8], data["error"])
            return None
        if not all(k in data for k in ("description", "matter", "confidence")):
            logger.warning("Classify API response missing required fields for chunk %s", chunk["id"][:8])
            return None

        logger.debug("API response for chunk %s: %s", chunk["id"][:8], data)
        return data
    except requests.RequestException as exc:
        logger.error("Classify API error for chunk %s: %s", chunk["id"], exc)
        return None


def _run_iteration():
    """Single classification iteration. Returns False if all chunks failed."""
    if _is_quiet_hours():
        logger.debug("Quiet hours — skipping classification")
        return None

    if not db.is_user_subscribed():
        logger.debug("User not subscribed — skipping classification")
        return None

    smoother.refresh_durations()
    smoother.consolidate_stagnant()

    chunks = chunker.get_unclassified_chunks()
    if not chunks:
        logger.info("No unclassified chunks — nothing to classify")
        return None

    # Partition: skip slots that already have a time_entry (avoid redundant
    # API calls and prevent the overwrite-then-fail cycle that creates gaps).
    pending_chunks: list[dict] = []
    already_done_chunks: list[dict] = []
    for chunk in chunks:
        start_dt = datetime.fromisoformat(chunk["start_time"])
        slot_date = start_dt.strftime("%Y-%m-%d")
        slot_time = start_dt.strftime("%H:%M")
        if db.time_entry_exists_for_slot(slot_date, slot_time):
            already_done_chunks.append(chunk)
        else:
            pending_chunks.append(chunk)

    if already_done_chunks:
        logger.info(
            "Skipping %d chunk(s) whose slot already has a time_entry",
            len(already_done_chunks),
        )
    for chunk in already_done_chunks:
        _apply_slot_completion_for_chunk(chunk)

    if not pending_chunks:
        logger.info("All %d chunk(s) already have time_entries — nothing new to classify", len(chunks))
        return None

    matters = _load_matters()
    clients = _load_clients()

    classified_rows = []
    api_failures = 0
    for chunk in pending_chunks:
        result = _classify_chunk(chunk, matters, clients)
        if result is None:
            api_failures += 1
            logger.info(
                "Classify slot result=fail chunk_id=%s start_time=%s",
                (chunk.get("id") or "")[:8],
                chunk.get("start_time"),
            )
            continue

        logger.info(
            "Classify slot result=ok chunk_id=%s start_time=%s",
            (chunk.get("id") or "")[:8],
            chunk.get("start_time"),
        )

        description = result.get("description", "")
        matter = result.get("matter", "")
        confidence = result.get("confidence", 0)
        task_code = result.get("task_code", "")
        activity_code = result.get("activity_code", "")

        start_dt = datetime.fromisoformat(chunk["start_time"])
        duration_min = round(chunk["duration_s"] / 60, 2)

        row = {
            "date": start_dt.strftime("%Y-%m-%d"),
            "start_time": start_dt.strftime("%H:%M"),
            "duration": str(duration_min),
            "description": description,
            "matter": matter,
            "confidence": str(confidence),
            "reviewed": "no",
            "outstandingFlags": "",
            "task_code": task_code,
            "activity_code": activity_code,
        }
        classified_rows.append({"row": row, "chunk": chunk})

        # Write time_entry FIRST, then chunk + slot completion, so a crash
        # can never leave events marked classified without their time_entry.
        try:
            conf_float = float(confidence)
        except (ValueError, TypeError):
            conf_float = 0.0
        oflags = row.get("outstandingFlags") or ""
        eid = db.insert_time_entry({
            "date": row["date"],
            "start_time": row["start_time"],
            "duration_min": duration_min,
            "description": description,
            "matter": matter,
            "confidence": conf_float,
            "reviewed": 0,
            "outstanding_flags": oflags,
            "task_code": task_code,
            "activity_code": activity_code,
            "chunk_id": chunk["id"],
        })
        try:
            from ardentrack import supabase_sync

            supabase_sync.push_time_entry(
                {
                    "id": eid,
                    "date": row["date"],
                    "start_time": row["start_time"],
                    "duration_min": duration_min,
                    "description": description,
                    "matter": matter,
                    "confidence": conf_float,
                    "reviewed": 0,
                    "outstanding_flags": oflags,
                    "task_code": task_code,
                    "activity_code": activity_code,
                    "chunk_id": chunk["id"],
                }
            )
        except Exception:
            pass

        db.insert_chunk({
            "id": chunk["id"],
            "start_time": chunk["start_time"],
            "end_time": chunk["end_time"],
            "duration_s": chunk["duration_s"],
            "event_ids": chunk["event_ids"],
            "matter_hint": chunk.get("matter_hint"),
            "classified": 1,
        })
        _apply_slot_completion_for_chunk(chunk)

    _flag_low_confidence([item["row"] for item in classified_rows])

    if classified_rows:
        db.sync_sheet_csv_from_db()
        logger.info(
            "Classified %d / %d pending chunks (%d skipped, %d failed), wrote entries to time_entries + sheet.csv",
            len(classified_rows), len(pending_chunks), len(already_done_chunks), api_failures,
        )
        _notify_flask_sheet_changed()

    db.insert_telemetry(
        event_type="classify_cycle",
        events_collected=len(chunks),
        chunks_classified=len(classified_rows),
        uptime_min=_get_uptime_min(),
    )

    return len(classified_rows) > 0


def run(stop_event: threading.Event):
    logger.info("Classification loop started (interval=%ds)", CLASSIFY_INTERVAL)

    consecutive_failures = 0
    last_event_count = 0

    while not stop_event.is_set():
        try:
            stale_min = _env_int("ARDEN_CLASSIFY_EVENT_AGE_MIN", 2)
            current_events = len(db.get_unclassified_events(older_than_minutes=stale_min))
            logger.info("Classify tick — %d unclassified events (>%dmin old)", current_events, stale_min)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES and current_events <= last_event_count:
                logger.info(
                    "Backing off — %d consecutive API failures, no new events",
                    consecutive_failures,
                )
            else:
                had_success = _run_iteration()
                if had_success is False:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                last_event_count = current_events
        except Exception:
            logger.exception("Error in classification iteration")
            consecutive_failures += 1

        stop_event.wait(CLASSIFY_INTERVAL)

    logger.info("Classification loop stopped")
