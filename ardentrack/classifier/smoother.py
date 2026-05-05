"""
ArdenTrack — Event smoother.

Provides two post-processing passes over recent window events:
  1. refresh_durations  — recalculate duration_s from inter-event gaps
  2. consolidate_stagnant — merge consecutive identical-window events
"""

import logging
from datetime import datetime, timedelta

from tzlocal import get_localzone

from ardentrack import db

logger = logging.getLogger(__name__)

_TZ = get_localzone()

STAGNANT_PATTERNS = [
    "zoom.exe",
    "teams.exe",
    "slack.exe",
    "discord.exe",
    "webex.exe",
    "gotomeeting.exe",
    "skype.exe",
    "spotify.exe",
    "vlc.exe",
    "wmplayer.exe",
    "movies & tv",
    "netflix",
    "youtube",
    "plex",
    "mpc-hc.exe",
    "mpc-hc64.exe",
    "foobar2000.exe",
    "itunes.exe",
    "musicbee.exe",
]

CONSOLIDATION_GAP = 30
STAGNANT_GAP = 120


def _parse_ts(iso_str: str) -> datetime:
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return datetime.now(tz=_TZ)


def refresh_durations():
    events = db.get_recent_window_events(minutes=15)
    if not events:
        return

    updates = 0
    for i in range(len(events) - 1):
        current = events[i]
        next_ev = events[i + 1]

        ts_current = _parse_ts(current["timestamp"])
        ts_next = _parse_ts(next_ev["timestamp"])
        gap = (ts_next - ts_current).total_seconds()

        if gap < 0:
            continue

        new_duration = round(gap, 2)
        if current["duration_s"] is None or abs(current["duration_s"] - new_duration) > 0.5:
            db.update_event(current["id"], {"duration_s": new_duration})
            updates += 1

    # The last event has no successor yet — extend it to cover the current
    # time so the overlap chunker produces chunks for all wall slots the
    # active window touches.  Without this, a long Zoom session creates one
    # short event that gets classified after its first slot, orphaning the
    # slots the extended duration would have covered.
    last = events[-1]
    ts_last = _parse_ts(last["timestamp"])
    now = datetime.now(tz=_TZ)
    live_duration = round((now - ts_last).total_seconds(), 2)
    if live_duration > 0 and (last["duration_s"] is None or live_duration > last["duration_s"] + 0.5):
        db.update_event(last["id"], {"duration_s": live_duration})
        updates += 1

    if updates:
        logger.debug("refresh_durations: updated %d events", updates)


def consolidate_stagnant():
    """Identify chains of consecutive identical-window events.

    Previously this function deleted the trailing events and inflated the
    keeper's duration.  That caused entire 6-minute chunking windows to become
    empty, producing gaps in the classified timesheet.

    Now the function is intentionally a **read-only diagnostic pass**: it logs
    what *would* be consolidated but leaves every event intact so the chunker
    still sees them in their original time windows.
    """
    events = db.get_recent_window_events(minutes=15)
    if len(events) < 2:
        return

    merged_count = 0
    i = 0
    while i < len(events) - 1:
        current = events[i]
        j = i + 1

        chain_ids = []

        while j < len(events):
            next_ev = events[j]

            if (
                next_ev["app_name"] != current["app_name"]
                or next_ev["window_title"] != current["window_title"]
            ):
                break

            ts_current_end = _parse_ts(current["timestamp"])
            total_duration = current["duration_s"] or 0
            if current["duration_s"]:
                ts_current_end += timedelta(seconds=total_duration)

            ts_next = _parse_ts(next_ev["timestamp"])
            gap = (ts_next - ts_current_end).total_seconds()

            app_lower = (current["app_name"] or "").lower()
            max_gap = STAGNANT_GAP if app_lower in STAGNANT_PATTERNS else CONSOLIDATION_GAP

            if gap > max_gap:
                break

            chain_ids.append(next_ev["id"])
            j += 1

        if chain_ids:
            merged_count += len(chain_ids)

        i = j

    if merged_count:
        logger.debug(
            "consolidate_stagnant: detected %d events that would merge (no-op)",
            merged_count,
        )
