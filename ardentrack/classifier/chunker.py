"""
ArdenTrack — Event chunker.

Groups unclassified window events into fixed 6-minute clock-aligned windows
for classification.  Each chunk always represents exactly 6 minutes of time,
aligned to wall-clock boundaries (e.g. :00, :06, :12, :18, :24, :30, :36,
:42, :48, :54).

By default (ARDEN_CHUNK_OVERLAP_MODE=1), an event is included in every 6-minute
bucket whose wall-clock interval intersects the event's [timestamp, timestamp+duration),
so long foreground sessions (e.g. Zoom) still produce a chunk per slot. Legacy
behavior (bucket by event start time only) is available with ARDEN_CHUNK_OVERLAP_MODE=0.
"""

import logging
import os
import uuid as _uuid
from collections import Counter
from datetime import datetime, timedelta

from tzlocal import get_localzone

from ardentrack import db

logger = logging.getLogger(__name__)

_TZ = get_localzone()

CHUNK_WINDOW = 360    # 6 minutes in seconds


def _parse_ts(iso_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
        return dt
    except (ValueError, TypeError):
        return datetime.now(tz=_TZ)


def _floor_to_6min(dt: datetime) -> datetime:
    """Round a datetime down to the nearest 6-minute boundary."""
    return dt.replace(minute=(dt.minute // 6) * 6, second=0, microsecond=0)


def _overlap_mode_enabled() -> bool:
    raw = (os.environ.get("ARDEN_CHUNK_OVERLAP_MODE") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _event_end_dt(ev: dict, start: datetime | None = None) -> datetime:
    ts = start if start is not None else _parse_ts(ev["timestamp"])
    try:
        dur = float(ev.get("duration_s") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return ts + timedelta(seconds=dur)


def _event_intersects_slot(ev: dict, slot_start: datetime, slot_end: datetime) -> bool:
    ts = _parse_ts(ev["timestamp"])
    ev_end = _event_end_dt(ev, ts)
    return ts < slot_end and ev_end > slot_start


def _slot_range_for_events(events: list) -> tuple[datetime, datetime] | None:
    if not events:
        return None
    min_ts = min(_parse_ts(e["timestamp"]) for e in events)
    max_end = max(_event_end_dt(e, _parse_ts(e["timestamp"])) for e in events)
    first_slot = _floor_to_6min(min_ts)
    last_slot = _floor_to_6min(max_end - timedelta(microseconds=1))
    if last_slot < first_slot:
        last_slot = first_slot
    return first_slot, last_slot


def required_slot_starts_for_event(ev: dict) -> list[str]:
    """
    ISO timestamps for each 6-minute wall slot that must classify successfully
    before this event can be marked classified.

    Must stay aligned with _build_chunks_overlap / _build_chunks_from_events.
    """
    if not _overlap_mode_enabled():
        ev_ts = _parse_ts(ev["timestamp"])
        window_start = _floor_to_6min(ev_ts)
        return [window_start.isoformat()]

    ts = _parse_ts(ev["timestamp"])
    ev_end = _event_end_dt(ev, ts)
    first_slot = _floor_to_6min(ts)
    last_slot = _floor_to_6min(ev_end - timedelta(microseconds=1))
    if last_slot < first_slot:
        last_slot = first_slot

    out: list[str] = []
    slot_start = first_slot
    while slot_start <= last_slot:
        slot_end = slot_start + timedelta(seconds=CHUNK_WINDOW)
        if _event_intersects_slot(ev, slot_start, slot_end):
            out.append(slot_start.isoformat())
        slot_start += timedelta(seconds=CHUNK_WINDOW)
    return out


def _build_chunks_overlap(events: list) -> list:
    """One chunk per 6-minute slot that intersects at least one event interval."""
    if not events:
        return []

    span = _slot_range_for_events(events)
    if not span:
        return []
    first_slot, last_slot = span

    chunks = []
    slot_start = first_slot
    while slot_start <= last_slot:
        slot_end = slot_start + timedelta(seconds=CHUNK_WINDOW)
        overlapping = [ev for ev in events if _event_intersects_slot(ev, slot_start, slot_end)]
        if overlapping:
            chunks.append({
                "id": str(_uuid.uuid4()),
                "start_time": slot_start.isoformat(),
                "end_time": slot_end.isoformat(),
                "duration_s": CHUNK_WINDOW,
                "event_ids": [ev["id"] for ev in overlapping],
                "matter_hint": _dominant_matter_hint(overlapping),
                "events": overlapping,
            })
        slot_start += timedelta(seconds=CHUNK_WINDOW)

    return chunks


def _dominant_matter_hint(events: list) -> str | None:
    hints = []
    for ev in events:
        hint = ev.get("matter_hint")
        conf = ev.get("matter_hint_confidence") or 0.0
        if hint:
            hints.append((hint, conf))

    if not hints:
        return None

    max_conf = max(h[1] for h in hints)
    top_hints = [h[0] for h in hints if h[1] >= max_conf - 0.05]

    if top_hints:
        counter = Counter(top_hints)
        return counter.most_common(1)[0][0]

    counter = Counter(h[0] for h in hints)
    return counter.most_common(1)[0][0]


def _build_chunks_from_events(events: list) -> list:
    """Bucket events into fixed 6-minute clock-aligned windows."""
    if not events:
        return []

    buckets: dict[datetime, list] = {}
    for ev in events:
        ev_ts = _parse_ts(ev["timestamp"])
        window_start = _floor_to_6min(ev_ts)
        buckets.setdefault(window_start, []).append(ev)

    chunks = []
    for window_start in sorted(buckets):
        window_end = window_start + timedelta(seconds=CHUNK_WINDOW)
        window_events = buckets[window_start]

        chunks.append({
            "id": str(_uuid.uuid4()),
            "start_time": window_start.isoformat(),
            "end_time": window_end.isoformat(),
            "duration_s": CHUNK_WINDOW,
            "event_ids": [ev["id"] for ev in window_events],
            "matter_hint": _dominant_matter_hint(window_events),
            "events": window_events,
        })

    return chunks


def _event_age_minutes() -> int:
    """Align with classify.run: ARDEN_CLASSIFY_EVENT_AGE_MIN (default 2)."""
    raw = os.environ.get("ARDEN_CLASSIFY_EVENT_AGE_MIN", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def _only_classify_closed_windows() -> bool:
    """If True (default), skip chunks whose wall-clock window has not ended yet."""
    raw = (os.environ.get("ARDEN_CLASSIFY_ONLY_CLOSED_WINDOWS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _chunk_wall_clock_window_has_ended(chunk: dict) -> bool:
    """True when now is at or past the chunk's end_time (6-minute slot closed)."""
    end = _parse_ts(chunk["end_time"])
    now = datetime.now(tz=_TZ)
    return now >= end


def _filter_afk_chunks(chunks: list) -> list:
    """Remove chunks whose 6-minute slot falls entirely within an AFK interval."""
    if not chunks:
        return chunks
    earliest = min(c["start_time"] for c in chunks)
    latest = max(c["end_time"] for c in chunks)
    afk_events = db.get_events_by_source("afk", earliest, latest)
    if not afk_events:
        return chunks

    intervals: list[tuple[datetime, datetime]] = []
    afk_start: datetime | None = None
    for ev in sorted(afk_events, key=lambda e: e["timestamp"]):
        is_afk = bool(ev.get("is_afk"))
        ts = _parse_ts(ev["timestamp"])
        if is_afk and afk_start is None:
            afk_start = ts
        elif not is_afk and afk_start is not None:
            intervals.append((afk_start, ts))
            afk_start = None
    if afk_start is not None:
        intervals.append((afk_start, _parse_ts(latest) + timedelta(seconds=CHUNK_WINDOW)))

    def slot_in_afk(chunk: dict) -> bool:
        s = _parse_ts(chunk["start_time"])
        e = _parse_ts(chunk["end_time"])
        for afk_s, afk_e in intervals:
            if afk_s <= s and e <= afk_e:
                return True
        return False

    before = len(chunks)
    kept = [c for c in chunks if not slot_in_afk(c)]
    dropped = before - len(kept)
    if dropped:
        logger.info("AFK filter: removed %d chunk(s) that fell entirely within AFK periods", dropped)
    return kept


def get_unclassified_chunks(older_than_minutes: int | None = None) -> list:
    age = older_than_minutes if older_than_minutes is not None else _event_age_minutes()
    events = db.get_unclassified_events(older_than_minutes=age)
    if not events:
        return []

    events.sort(key=lambda e: e["timestamp"])
    if _overlap_mode_enabled():
        chunks = _build_chunks_overlap(events)
        mode = "overlap"
    else:
        chunks = _build_chunks_from_events(events)
        mode = "start_bucket"

    if _only_classify_closed_windows():
        before = len(chunks)
        chunks = [c for c in chunks if _chunk_wall_clock_window_has_ended(c)]
        skipped = before - len(chunks)
        if skipped:
            logger.info(
                "Chunker: withheld %d chunk(s) whose 6-minute window is not over yet (closed-window mode)",
                skipped,
            )

    chunks = _filter_afk_chunks(chunks)

    logger.info(
        "Chunker (%s) produced %d chunks from %d unclassified events",
        mode, len(chunks), len(events),
    )
    return chunks
