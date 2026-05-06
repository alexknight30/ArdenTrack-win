"""
Supabase token storage (Windows Credential Manager via keyring) and refresh.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ardentrack import db

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "ArdenTrack"
KR_ACCESS = "access_token"
KR_REFRESH = "refresh_token"
KR_EXPIRY = "token_expiry"

_TOKEN_REFRESH_MARGIN_SEC = 300
_refresh_lock = threading.Lock()


def _jwt_payload_unverified(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        pad = "=" * ((4 - len(payload_b64) % 4) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _load_keyring():
    import keyring

    return keyring


_CRED_CHUNK_SIZE = 1200


def _set_chunked(kr, service: str, key: str, value: str) -> None:
    """Store a value that may exceed Windows Credential Manager's byte limit."""
    _delete_chunked(kr, service, key)
    if len(value) <= _CRED_CHUNK_SIZE:
        kr.set_password(service, key, value)
        return
    chunks = [value[i:i + _CRED_CHUNK_SIZE] for i in range(0, len(value), _CRED_CHUNK_SIZE)]
    kr.set_password(service, key, f"__chunked__:{len(chunks)}")
    for i, chunk in enumerate(chunks):
        kr.set_password(service, f"{key}__chunk_{i}", chunk)


def _get_chunked(kr, service: str, key: str) -> str | None:
    raw = kr.get_password(service, key)
    if raw is None:
        return None
    if not raw.startswith("__chunked__:"):
        return raw
    n = int(raw.split(":")[1])
    parts = []
    for i in range(n):
        part = kr.get_password(service, f"{key}__chunk_{i}")
        if part is None:
            return None
        parts.append(part)
    return "".join(parts)


def _delete_chunked(kr, service: str, key: str) -> None:
    try:
        raw = kr.get_password(service, key)
        if raw and raw.startswith("__chunked__:"):
            n = int(raw.split(":")[1])
            for i in range(n):
                try:
                    kr.delete_password(service, f"{key}__chunk_{i}")
                except Exception:
                    pass
        kr.delete_password(service, key)
    except Exception:
        pass


def store_tokens(
    access_token: str,
    refresh_token: str,
    expires_in: int | float,
) -> None:
    """Persist tokens and update auth_user metadata from JWT (no verify)."""
    kr = _load_keyring()
    expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
    expiry_iso = expiry_dt.isoformat()

    _set_chunked(kr, KEYRING_SERVICE, KR_ACCESS, access_token)
    _set_chunked(kr, KEYRING_SERVICE, KR_REFRESH, refresh_token)
    kr.set_password(KEYRING_SERVICE, KR_EXPIRY, expiry_iso)

    claims = _jwt_payload_unverified(access_token)
    meta = claims.get("user_metadata") or {}
    sub = claims.get("sub")
    email = claims.get("email")
    name = meta.get("full_name") or meta.get("name") or claims.get("name")
    firm_name = meta.get("firm_name")
    billing_status = meta.get("billing_status")
    if billing_status is None and isinstance(meta.get("subscription"), dict):
        billing_status = meta["subscription"].get("status")
    if billing_status is None:
        existing = db.get_auth_user_row()
        if existing:
            billing_status = existing.get("billing_status")

    is_premium = 1 if str(meta.get("tier", "")).lower() in ("premium", "paid") else 0

    try:
        db.upsert_auth_user(
            user_id=sub,
            email=email,
            name=name,
            firm_name=firm_name,
            billing_status=billing_status,
            is_premium=is_premium,
        )
    except Exception:
        logger.exception("upsert_auth_user after store_tokens")


def get_refresh_token() -> str | None:
    try:
        kr = _load_keyring()
        return _get_chunked(kr, KEYRING_SERVICE, KR_REFRESH)
    except Exception:
        return None


def get_access_token() -> str | None:
    try:
        kr = _load_keyring()
        return _get_chunked(kr, KEYRING_SERVICE, KR_ACCESS)
    except Exception:
        return None


def get_token_expiry() -> datetime | None:
    try:
        kr = _load_keyring()
        raw = kr.get_password(KEYRING_SERVICE, KR_EXPIRY)
        if not raw:
            return None
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def clear_tokens() -> None:
    kr = _load_keyring()
    _delete_chunked(kr, KEYRING_SERVICE, KR_ACCESS)
    _delete_chunked(kr, KEYRING_SERVICE, KR_REFRESH)
    try:
        kr.delete_password(KEYRING_SERVICE, KR_EXPIRY)
    except Exception:
        pass


def is_token_expired() -> bool:
    exp = get_token_expiry()
    if exp is None:
        return True
    return datetime.now(timezone.utc) >= exp - timedelta(seconds=_TOKEN_REFRESH_MARGIN_SEC)


def has_credentials() -> bool:
    return bool(get_refresh_token())


def _refresh_access_token() -> str | None:
    url_base = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    anon = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    refresh = get_refresh_token()
    if not url_base or not anon or not refresh:
        return None

    token_url = f"{url_base}/auth/v1/token?grant_type=refresh_token"
    try:
        resp = requests.post(
            token_url,
            headers={
                "apikey": anon,
                "Content-Type": "application/json",
            },
            json={"refresh_token": refresh},
            timeout=30,
        )
        if not resp.ok:
            logger.warning("Token refresh failed: %s %s", resp.status_code, resp.text[:200])
            clear_tokens()
            return None
        data = resp.json()
        access = data.get("access_token")
        new_refresh = data.get("refresh_token") or refresh
        expires_in = float(data.get("expires_in") or 3600)
        if not access:
            clear_tokens()
            return None
        store_tokens(access, new_refresh, expires_in)
        return access
    except Exception:
        logger.exception("Token refresh error")
        clear_tokens()
        return None


def get_valid_token() -> str | None:
    """Return a usable access JWT, refreshing if needed. Never raises.

    A lock serialises the expired-check + refresh so concurrent callers
    don't each fire a separate refresh (Supabase invalidates a refresh
    token after its first use).
    """
    try:
        if not has_credentials():
            return None
        with _refresh_lock:
            access = get_access_token()
            if access and not is_token_expired():
                return access
            return _refresh_access_token()
    except Exception:
        logger.exception("get_valid_token")
        return None
