"""Per-user daily quota for AI scans (server OpenRouter key). Free vs Premium.

Free users can earn extra scans by watching a rewarded ad (POST /api/ai/reward), capped per day.

Users can opt in to a TRIAL_DAYS Premium trial once (POST /api/ai/trial); it is never automatic.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, Form, HTTPException

from auth import get_current_user
from database import get_db

FREE_DAILY = int(os.environ.get("AI_FREE_SCANS_PER_DAY", "5"))
PREMIUM_DAILY = int(os.environ.get("AI_PREMIUM_SCANS_PER_DAY", "15"))  # priced for a 10% margin at this cap (see the app repo .product/ECONOMICS.md)
MAX_REWARDS_PER_DAY = int(os.environ.get("AI_MAX_REWARDS_PER_DAY", "5"))
TRIAL_DAYS = int(os.environ.get("PREMIUM_TRIAL_DAYS", "3"))


async def trial_ends_at(db, user_id: str) -> datetime | None:
    """Premium (no ads, full limits) for TRIAL_DAYS, only after the user taps "start trial"
    (start_trial). One per account: users.trial_started_at is never cleared."""
    try:
        u = await db.users.find_one({"_id": ObjectId(user_id)}, {"trial_started_at": 1})
    except InvalidId:
        return None
    start = (u or {}).get("trial_started_at")
    if not start:
        return None
    end = start.replace(tzinfo=timezone.utc) + timedelta(days=TRIAL_DAYS)
    return end if end > datetime.now(timezone.utc) else None


async def start_trial(db, user_id: str) -> None:
    """Idempotent: a second call (or a reinstall) never restarts the clock."""
    await db.users.update_one(
        {"_id": ObjectId(user_id), "trial_started_at": {"$exists": False}},
        {"$set": {"trial_started_at": datetime.now(timezone.utc)}},
    )


async def trial_available(db, user_id: str) -> bool:
    try:
        u = await db.users.find_one({"_id": ObjectId(user_id)}, {"trial_started_at": 1})
    except InvalidId:
        return False
    return bool(u) and not u.get("trial_started_at")


async def _premium(db, user_id: str) -> bool:
    from services.entitlements import is_premium
    return await trial_ends_at(db, user_id) is not None or await is_premium(db, user_id)


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
            "used": 0, "bonus": 0, "rewards": 0,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
        }},
        upsert=True,
    )


async def status(db, user_id: str, profile: dict | None) -> dict:
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    doc = await db.ai_usage.find_one({"user_id": user_id, "date": day})
    premium = await _premium(db, user_id)
    trial_end = await trial_ends_at(db, user_id)
    limit = (PREMIUM_DAILY if premium else FREE_DAILY) + doc["bonus"]
    return {
        "premium": premium,
        "trial_ends_at": trial_end.isoformat() if trial_end else None,
        "trial_available": await trial_available(db, user_id),
        "limit": limit,
        "free_limit": FREE_DAILY,        # so the paywall quotes the real numbers if the env changes them
        "premium_limit": PREMIUM_DAILY,
        "used": doc["used"],
        "remaining": max(limit - doc["used"], 0),
        "rewards_left": max(MAX_REWARDS_PER_DAY - doc["rewards"], 0),
    }


async def consume(db, user_id: str, profile: dict | None) -> None:
    """Take one scan from today's quota or raise 429 (structured detail the app keys off)."""
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    base = PREMIUM_DAILY if await _premium(db, user_id) else FREE_DAILY
    res = await db.ai_usage.update_one(
        {"user_id": user_id, "date": day,
         "$expr": {"$lt": ["$used", {"$add": [base, "$bonus"]}]}},
        {"$inc": {"used": 1}},
    )
    if res.modified_count == 0:
        st = await status(db, user_id, profile)
        raise HTTPException(429, detail={
            "code": "ai_quota_exhausted",
            "message": "You've used today's free AI scans.",
            "limit": st["limit"],
            "rewards_left": st["rewards_left"],
        })


async def refund(db, user_id: str, profile: dict | None) -> None:
    """Give the scan back when the AI call itself failed."""
    await db.ai_usage.update_one(
        {"user_id": user_id, "date": _today(profile), "used": {"$gt": 0}},
        {"$inc": {"used": -1}},
    )


async def grant_reward(db, user_id: str, profile: dict | None) -> dict:
    # ponytail: trusts the client after a rewarded ad; per-day cap bounds abuse.
    # Upgrade to AdMob server-side verification (SSV) if abuse shows up.
    if await _premium(db, user_id):
        raise HTTPException(400, "Premium has no ads — no bonus scans needed")
    day = _today(profile)
    await _ensure_doc(db, user_id, day)
    res = await db.ai_usage.update_one(
        {"user_id": user_id, "date": day, "rewards": {"$lt": MAX_REWARDS_PER_DAY}},
        {"$inc": {"bonus": 1, "rewards": 1}},
    )
    if res.modified_count == 0:
        raise HTTPException(429, "No more bonus scans available today")
    return await status(db, user_id, profile)


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
