"""Usage limits for every AI feature and for photo uploads. Nothing is unlimited: free < premium, both capped.

AI scans keep their own daily quota (services/ai_quota.py). This covers the rest:
  photo_compare    AI body-photo comparison, per calendar month
  weekly_insights  AI weekly insights generation (cached reads are free), per calendar month
  photos           stored progress + weight photos, a total cap, plus a monthly upload cap for premium
Trial accounts count as premium (same rule as ai_quota). All numbers are env-tunable.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from pymongo.errors import DuplicateKeyError

from auth import get_current_user
from database import get_db
from services import ai_quota


def _n(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# feature -> (free per month, premium per month)
MONTHLY = {
    "photo_compare": (_n("LIMIT_COMPARE_FREE", 2), _n("LIMIT_COMPARE_PREMIUM", 15)),
    "weekly_insights": (_n("LIMIT_INSIGHTS_FREE", 1), _n("LIMIT_INSIGHTS_PREMIUM", 10)),
}
LABEL = {"photo_compare": "AI photo comparisons", "weekly_insights": "AI weekly insights"}
PHOTOS_TOTAL = (_n("LIMIT_PHOTOS_FREE", 20), _n("LIMIT_PHOTOS_PREMIUM", 300))
PHOTOS_PREMIUM_PER_MONTH = _n("LIMIT_PHOTOS_PREMIUM_MONTHLY", 60)
_PHOTO_COLLECTIONS = ("body_photos", "weight_photos")


def _limit_error(feature: str, limit: int, premium: bool, period: str) -> HTTPException:
    what = LABEL.get(feature, "photos")
    msg = (f"You've reached your {period}ly limit of {limit} {what}." if feature in MONTHLY
           else f"You've reached the limit of {limit} {what}.")
    msg += " It resets next month." if premium and feature in MONTHLY else ""
    msg += "" if premium else " Upgrade to Premium for a higher limit."
    return HTTPException(429, detail={
        "code": "limit_reached", "feature": feature, "limit": limit, "premium": premium, "message": msg,
    })


def _month(profile: dict | None) -> str:
    return ai_quota._today(profile)[:7]  # YYYY-MM in the user's timezone


async def consume(db, user_id: str, profile: dict | None, feature: str) -> None:
    """Take one use of a monthly-capped feature or raise 429."""
    premium = await ai_quota._premium(db, user_id)
    limit = MONTHLY[feature][1 if premium else 0]
    key = {"user_id": user_id, "feature": feature, "period": _month(profile)}
    try:
        await db.feature_usage.update_one(
            key, {"$setOnInsert": {"used": 0, "expires_at": datetime.now(timezone.utc) + timedelta(days=70)}}, upsert=True
        )
    except DuplicateKeyError:
        pass  # a concurrent request created it first
    res = await db.feature_usage.update_one({**key, "used": {"$lt": limit}}, {"$inc": {"used": 1}})
    if res.modified_count == 0:
        raise _limit_error(feature, limit, premium, "month")


async def refund(db, user_id: str, profile: dict | None, feature: str) -> None:
    await db.feature_usage.update_one(
        {"user_id": user_id, "feature": feature, "period": _month(profile), "used": {"$gt": 0}}, {"$inc": {"used": -1}}
    )


def gate(feature: str):
    """Dependency: take a use up front, give it back if the handler raises (the AI call failed)."""
    async def dep(user_id: str = Depends(get_current_user)):
        db = get_db()
        profile = await db.user_profile.find_one({"user_id": user_id})
        await consume(db, user_id, profile, feature)
        try:
            yield
        except Exception:
            await refund(db, user_id, profile, feature)
            raise
    return dep


async def _photo_counts(db, user_id: str, profile: dict | None) -> tuple[int, int]:
    """(photos stored, photos uploaded this month). Weight-only entries have no image and don't count."""
    first = datetime.fromisoformat(_month(profile) + "-01").replace(tzinfo=timezone.utc)
    total = month = 0
    for c in _PHOTO_COLLECTIONS:
        has_img = {"user_id": user_id, "image_url": {"$ne": None}}
        total += await db[c].count_documents(has_img)
        # ponytail: counted from stored docs, so delete + re-upload frees a slot. Fine for a storage cap.
        month += await db[c].count_documents({**has_img, "created_at": {"$gte": first}})
    return total, month


async def check_photo_upload(db, user_id: str, profile: dict | None) -> None:
    premium = await ai_quota._premium(db, user_id)
    total, month = await _photo_counts(db, user_id, profile)
    cap = PHOTOS_TOTAL[1 if premium else 0]
    if total >= cap:
        raise _limit_error("photos", cap, premium, "total")
    if premium and month >= PHOTOS_PREMIUM_PER_MONTH:
        raise _limit_error("photos", PHOTOS_PREMIUM_PER_MONTH, premium, "month")


async def status(db, user_id: str, profile: dict | None) -> dict:
    premium = await ai_quota._premium(db, user_id)
    month = _month(profile)
    out = {"premium": premium, "period": month}
    for f, (free, prem) in MONTHLY.items():
        doc = await db.feature_usage.find_one({"user_id": user_id, "feature": f, "period": month})
        limit, used = (prem if premium else free), (doc or {}).get("used", 0)
        out[f] = {"limit": limit, "used": used, "remaining": max(limit - used, 0)}
    total, uploaded = await _photo_counts(db, user_id, profile)
    cap = PHOTOS_TOTAL[1 if premium else 0]
    out["photos"] = {"limit": cap, "used": total, "remaining": max(cap - total, 0),
                     "monthly_limit": PHOTOS_PREMIUM_PER_MONTH if premium else None, "uploaded_this_month": uploaded}
    return out
