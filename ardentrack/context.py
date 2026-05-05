"""
ArdenTrack — User context file management.

Manages userdata/user_context.json which is sent with every classify call
to personalise the classifier with correction patterns, matter summaries,
and preferred billing codes.
"""

import json
import logging
import os
from datetime import datetime

from tzlocal import get_localzone

from ardentrack.paths import USERDATA_DIR

logger = logging.getLogger(__name__)

_TZ = get_localzone()

_CONTEXT_PATH = os.path.join(USERDATA_DIR, "user_context.json")

_DEFAULT_CONTEXT = {
    "updated_at": "",
    "profile": {
        "firm_name": "",
        "timekeeper_name": "",
        "narrative_style": "concise",
        "practice_areas": [],
    },
    "matters_summary": [],
    "correction_patterns": [],
    "preferred_task_codes": {},
}


def load_context() -> dict:
    """Load user_context.json or return the empty default structure."""
    if not os.path.exists(_CONTEXT_PATH):
        return dict(_DEFAULT_CONTEXT)
    try:
        with open(_CONTEXT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read user_context.json, returning default: %s", exc)
        return dict(_DEFAULT_CONTEXT)


def _save_context(ctx: dict):
    ctx["updated_at"] = datetime.now(tz=_TZ).isoformat()
    os.makedirs(os.path.dirname(_CONTEXT_PATH), exist_ok=True)
    with open(_CONTEXT_PATH, "w", encoding="utf-8") as f:
        json.dump(ctx, f, indent=2, ensure_ascii=False)


def update_from_matters(matters_dict: dict):
    ctx = load_context()

    if isinstance(matters_dict, dict):
        matters_list = list(matters_dict.values())
    elif isinstance(matters_dict, list):
        matters_list = matters_dict
    else:
        logger.warning("Unexpected matters format: %s", type(matters_dict))
        return

    summaries = []
    for m in matters_list:
        summaries.append({
            "name": m.get("name", ""),
            "description": m.get("description", ""),
            "practice_area": m.get("practiceArea", "general"),
            "common_apps": [],
            "common_narratives": [],
        })

    ctx["matters_summary"] = summaries
    _save_context(ctx)
    logger.info("Updated matters_summary with %d matters", len(summaries))


def update_from_correction(
    original_matter: str,
    corrected_matter: str,
    app_name: str,
    title_keywords: list,
):
    ctx = load_context()
    patterns = ctx.get("correction_patterns", [])

    for pattern in patterns:
        if (
            pattern.get("original_matter") == original_matter
            and pattern.get("corrected_matter") == corrected_matter
        ):
            existing_apps = set(pattern.get("trigger_apps", []))
            if app_name:
                existing_apps.add(app_name)
            pattern["trigger_apps"] = sorted(existing_apps)

            existing_kw = set(pattern.get("trigger_title_keywords", []))
            existing_kw.update(kw.lower() for kw in title_keywords if kw)
            pattern["trigger_title_keywords"] = sorted(existing_kw)

            pattern["frequency"] = pattern.get("frequency", 1) + 1
            _save_context(ctx)
            logger.info(
                "Incremented correction pattern %s -> %s (freq %d)",
                original_matter, corrected_matter, pattern["frequency"],
            )
            return

    patterns.append({
        "original_matter": original_matter,
        "corrected_matter": corrected_matter,
        "trigger_apps": [app_name] if app_name else [],
        "trigger_title_keywords": [kw.lower() for kw in title_keywords if kw],
        "frequency": 1,
    })
    ctx["correction_patterns"] = patterns
    _save_context(ctx)
    logger.info("Added new correction pattern %s -> %s", original_matter, corrected_matter)
