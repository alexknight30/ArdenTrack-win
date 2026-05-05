"""
Push/pull sync between local SQLite and Supabase (never raises from callers).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from supabase import create_client

from ardentrack import auth, db

logger = logging.getLogger(__name__)

_SKIP_CLOUD_KEYS = frozenset({"user_id"})


def _get_client():
    try:
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        anon = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        if not url or not anon:
            return None
        access = auth.get_valid_token()
        refresh = auth.get_refresh_token()
        if not access or not refresh:
            return None
        client = create_client(url, anon)
        client.auth.set_session(access, refresh)
        return client
    except Exception:
        logger.exception("_get_client")
        return None


def _get_user_id() -> str | None:
    try:
        row = db.get_auth_user_row()
        if not row:
            return None
        uid = row.get("user_id")
        return str(uid) if uid else None
    except Exception:
        logger.exception("_get_user_id")
        return None


def _merge_row(
    local: dict | None,
    cloud: dict[str, Any],
    columns: list[str],
    pk_key: str,
) -> dict[str, Any]:
    """Start from local row, overlay non-null cloud fields (preserve local when cloud omits)."""
    out: dict[str, Any] = {}
    if local:
        for c in columns:
            if c in local:
                out[c] = local[c]
    for c in columns:
        if c not in cloud or c in _SKIP_CLOUD_KEYS:
            continue
        v = cloud.get(c)
        if c == pk_key:
            if v is not None:
                out[c] = v
        elif v is not None:
            out[c] = v
    if not out:
        for c in columns:
            if c in cloud and cloud.get(c) is not None:
                out[c] = cloud[c]
    return out


def push_time_entry(entry_dict: dict) -> None:
    try:
        client = _get_client()
        if not client:
            return
        uid = _get_user_id()
        if not uid:
            logger.warning("push_time_entry: no user_id in auth_user")
            return

        row = dict(entry_dict)
        eid = row.get("id")
        if not eid:
            return

        payload = {
            "id": eid,
            "user_id": uid,
            "date": row.get("date"),
            "start_time": row.get("start_time"),
            "duration_min": row.get("duration_min"),
            "description": row.get("description") or "",
            "matter": row.get("matter"),
            "confidence": row.get("confidence"),
            "reviewed": row.get("reviewed"),
            "outstanding_flags": row.get("outstanding_flags") or "",
            "task_code": row.get("task_code"),
            "activity_code": row.get("activity_code"),
            "chunk_id": row.get("chunk_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

        client.table("time_entries").upsert(
            payload,
            on_conflict="id",
        ).execute()
        db.mark_entry_synced(str(eid))
    except Exception:
        logger.exception("push_time_entry")


def pull_matters() -> None:
    try:
        client = _get_client()
        if not client:
            return
        res = client.table("matters").select("*").execute()
        rows = getattr(res, "data", None) or []
        for cloud in rows:
            try:
                name = cloud.get("name")
                if not name:
                    continue
                local = db.get_matter_by_name(str(name))
                merged = _merge_row(local, cloud, db.MATTER_COLUMNS, "name")
                if "name" not in merged:
                    merged["name"] = str(name)
                db.upsert_matter(merged)
            except Exception:
                logger.exception("pull_matters row")
    except Exception:
        logger.exception("pull_matters")


def pull_clients() -> None:
    try:
        client = _get_client()
        if not client:
            return
        res = client.table("clients").select("*").execute()
        rows = getattr(res, "data", None) or []
        for cloud in rows:
            try:
                name = cloud.get("name")
                if not name:
                    continue
                local = db.get_client_by_name(str(name))
                merged = _merge_row(local, cloud, db.CLIENT_COLUMNS, "name")
                if "name" not in merged:
                    merged["name"] = str(name)
                db.upsert_client(merged)
            except Exception:
                logger.exception("pull_clients row")
    except Exception:
        logger.exception("pull_clients")


def pull_profile() -> None:
    try:
        client = _get_client()
        if not client:
            return
        res = client.table("profile").select("*").limit(1).execute()
        raw = getattr(res, "data", None)
        if isinstance(raw, list):
            cloud = raw[0] if raw else None
        else:
            cloud = raw
        if not cloud:
            return
        local = db.get_profile_row()
        merged = _merge_row(local, cloud, db.PROFILE_COLUMNS, "id")
        merged["id"] = 1
        db.upsert_profile(merged)

        auth_row = db.get_auth_user_row()
        if auth_row and cloud.get("firm_name"):
            try:
                db.upsert_auth_user(
                    user_id=auth_row.get("user_id"),
                    email=auth_row.get("email"),
                    name=auth_row.get("name"),
                    firm_name=cloud.get("firm_name"),
                    billing_status=auth_row.get("billing_status"),
                    is_premium=int(auth_row.get("is_premium") or 0),
                )
            except Exception:
                logger.exception("pull_profile auth_user firm sync")
    except Exception:
        logger.exception("pull_profile")


def push_unsynced_entries() -> None:
    try:
        entries = db.get_unsynced_entries()
        for ent in entries:
            try:
                push_time_entry(ent)
            except Exception:
                logger.exception("push_unsynced_entries row")
    except Exception:
        logger.exception("push_unsynced_entries")
