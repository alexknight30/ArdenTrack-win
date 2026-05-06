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
            logger.debug("Supabase env vars missing — URL=%s, KEY=%s",
                         "set" if url else "MISSING", "set" if anon else "MISSING")
            return None
        access = auth.get_valid_token()
        refresh = auth.get_refresh_token()
        if not access or not refresh:
            logger.debug("No auth tokens — access=%s, refresh=%s",
                         "present" if access else "MISSING", "present" if refresh else "MISSING")
            return None
        logger.debug("Creating Supabase client for %s", url)
        client = create_client(url, anon)
        client.auth.set_session(access, refresh)
        logger.debug("Supabase session set successfully")
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
            logger.debug("pull_matters: skipped (no client)")
            return
        logger.debug("pull_matters: fetching from Supabase…")
        res = client.table("matters").select("*").execute()
        rows = getattr(res, "data", None) or []
        logger.info("pull_matters: got %d rows from Supabase", len(rows))
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
            logger.debug("pull_clients: skipped (no client)")
            return
        logger.debug("pull_clients: fetching from Supabase…")
        res = client.table("clients").select("*").execute()
        rows = getattr(res, "data", None) or []
        logger.info("pull_clients: got %d rows from Supabase", len(rows))
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
            logger.debug("pull_profile: skipped (no client)")
            return
        logger.debug("pull_profile: fetching from Supabase…")
        res = client.table("profile").select("*").limit(1).execute()
        raw = getattr(res, "data", None)
        if isinstance(raw, list):
            cloud = raw[0] if raw else None
        else:
            cloud = raw
        if not cloud:
            logger.info("pull_profile: no profile row found in Supabase")
            return
        logger.info("pull_profile: got profile from Supabase — firm=%s", cloud.get("firm_name"))
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


def pull_billing_status() -> None:
    """Fetch billing_status from the Supabase `profiles` (plural) table and update local auth_user."""
    try:
        client = _get_client()
        if not client:
            logger.info("pull_billing_status: skipped (no client)")
            return
        uid = _get_user_id()
        if not uid:
            logger.info("pull_billing_status: skipped (no user_id)")
            return
        res = client.table("profiles").select("billing_status").eq("id", uid).limit(1).execute()
        rows = getattr(res, "data", None) or []
        if not rows:
            logger.info("pull_billing_status: no profiles row found for user %s", uid[:8])
            return
        status = rows[0].get("billing_status")
        if status:
            logger.info("pull_billing_status: billing_status=%s", status)
            db.update_billing_status(status)
        else:
            logger.info("pull_billing_status: billing_status is null/empty in profiles")
    except Exception:
        logger.exception("pull_billing_status")


def push_unsynced_entries() -> None:
    try:
        entries = db.get_unsynced_entries()
        logger.info("push_unsynced_entries: %d entries to push", len(entries))
        for ent in entries:
            try:
                push_time_entry(ent)
            except Exception:
                logger.exception("push_unsynced_entries row")
    except Exception:
        logger.exception("push_unsynced_entries")
