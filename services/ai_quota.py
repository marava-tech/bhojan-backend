"""Per-user daily quota for AI scans (server OpenRouter key). Free vs Premium only, no ads.

New accounts get a WELCOME_DAYS boost so the first week shows the product working.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Optional

from fastapi import Depends, Form, HTTPException

from auth import get_current_user
from database import get_db

FREE_DAILY = int(os.environ.get("AI_FREE_SCANS_PER_DAY", "3"))
PREMIUM_DAILY = int(os.environ.get("AI_PREMIUM_SCANS_PER_DAY", "15"))
WELCOME_DAILY = int(os.environ.get("AI_WELCOME_SCANS_PER_DAY", "8"))
WELCOME_DAYS = int(os.environ.get("AI_WELCOME_DAYS", "7"))


def _free_daily(profile: dict | None) -> int:
    created = (profile or {}).get("created_at")
    if created:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created < timedelta(days=WELCOME_DAYS):
            return max(WELCOME_DAILY, FREE_DAILY)
    return FREE_DAILY


async def _premium(db, user_id: str) -> bool:
    # local import: entitlements imports this module (has_own_key)
    from services.entitlements import is_premium
    return await is_premium(db, user_id)


def _today(profile: dict | None) -> str:
    try:
        tz = ZoneInfo((profile or {}).get("user_timezone") or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


async def _ensure_doc(db, user_id: str, day: str) -> None:
    await db.ai_usage.update_one(
        {"user_id": user_id, "date": day},
        {"$setOnInsert": {
            "used": 0,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
        }},
        upsert=True,
    )


async def status(db, user_id: str, profile: dict | None) -> dict:
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    doc = await db.ai_usage.find_one({"user_id": user_id, "date": day})
    premium = await _premium(db, user_id)
    limit = PREMIUM_DAILY if premium else _free_daily(profile)
    return {
        "premium": premium,
        "limit": limit,
        "used": doc["used"],
        "remaining": max(limit - doc["used"], 0),
    }


async def consume(db, user_id: str, profile: dict | None) -> None:
    """Take one scan from today's quota or raise 429 (structured detail the app keys off)."""
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    base = PREMIUM_DAILY if await _premium(db, user_id) else _free_daily(profile)
    res = await db.ai_usage.update_one(
        {"user_id": user_id, "date": day, "used": {"$lt": base}},
        {"$inc": {"used": 1}},
    )
    if res.modified_count == 0:
        st = await status(db, user_id, profile)
        raise HTTPException(429, detail={
            "code": "ai_quota_exhausted",
            "message": "You've used today's free AI scans.",
            "limit": st["limit"],
        })


async def refund(db, user_id: str, profile: dict | None) -> None:
    """Give the scan back when the AI call itself failed."""
    await db.ai_usage.update_one(
        {"user_id": user_id, "date": _today(profile), "used": {"$gt": 0}},
        {"$inc": {"used": -1}},
    )


async def _take(user_id: str, count: bool):
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    if count:
        await consume(db, user_id, profile)
    return db, profile


# Dependencies: take a scan up front, give it back if the handler raises.
async def scan_gate(user_id: str = Depends(get_current_user)):
    db, profile = await _take(user_id, True)
    try:
        yield
    except Exception:
        await refund(db, user_id, profile)
        raise


async def photo_scan_gate(
    model: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user),
):
    # A retry with a different model re-scans the same photo, so it's free.
    count = not model
    db, profile = await _take(user_id, count)
    try:
        yield
    except Exception:
        if count:
            await refund(db, user_id, profile)
        raise
