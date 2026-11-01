"""Self-check for the audit fixes. MONGODB_URI=mongodb://127.0.0.1:27099 python -m scripts.check_audit_fixes"""
import asyncio
import os
import time

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("UPLOAD_URL", "http://127.0.0.1:1/upload")
os.environ.setdefault("UPLOAD_API_KEY", "x")
os.environ["AI_FREE_SCANS_PER_DAY"] = "2"

from bson import ObjectId  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import auth  # noqa: E402
from database import get_db  # noqa: E402
from routers import auth_router  # noqa: E402
from services import ai_quota, entitlements  # noqa: E402


async def raises(coro, status):
    try:
        await coro
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        return
    raise AssertionError(f"expected HTTP {status}")


async def gate_counts(uid, **form):
    """True if the photo gate took a scan from today's quota."""
    db = get_db()
    before = (await db.ai_usage.find_one({"user_id": uid}) or {}).get("used", 0)
    g = ai_quota.photo_scan_gate(user_id=uid, model=form.get("model"), image_url=form.get("image_url"))
    await g.__anext__()
    await g.aclose()
    return ((await db.ai_usage.find_one({"user_id": uid}) or {}).get("used", 0)) > before


class Req:
    headers = {"cf-connecting-ip": "1.2.3.4"}
    client = None


async def main():
    db = get_db()
    for c in ("users", "ai_usage", "scan_results", "entitlements", "trial_used", "user_profile"):
        await db[c].delete_many({})

    # quota: a made-up model is NOT a free retry; a real retry of this user's own scan is
    assert await gate_counts("q1", model="whatever", image_url="http://x/y.jpg")
    assert await gate_counts("q1", model="gpt-4o", image_url="http://x/unscanned.jpg")
    await db.scan_results.insert_one({"user_id": "q2", "image_url": "http://x/mine.jpg"})
    assert not await gate_counts("q2", model="gpt-4o", image_url="http://x/mine.jpg")
    assert await gate_counts("q3", model="gpt-4o", image_url="http://x/mine.jpg")  # someone else's scan

    # deleted account: token no longer works
    uid = str((await db.users.insert_one({"email": "a@b.co"})).inserted_id)
    await auth._require_user(uid)
    await db.users.delete_one({"_id": ObjectId(uid)})
    auth.forget_user(uid)
    await raises(auth._require_user(uid), 401)

    # trial is once per email, even across delete + sign up again
    u1 = (await db.users.insert_one({"email": "T@b.co"})).inserted_id
    await ai_quota.start_trial(db, str(u1))
    user = await db.users.find_one({"_id": u1})
    assert user.get("trial_started_at")
    await ai_quota.remember_trial_used(db, user)
    await db.users.delete_one({"_id": u1})
    u2 = (await db.users.insert_one({"email": "t@b.co"})).inserted_id
    assert not await ai_quota.trial_available(db, str(u2))
    await ai_quota.start_trial(db, str(u2))
    assert not (await db.users.find_one({"_id": u2})).get("trial_started_at")

    # webhook ordering: an older event arriving late must not shorten access
    def ev(kind, ts, days):
        return {"type": kind, "app_user_id": "w1", "entitlement_ids": ["premium"], "event_timestamp_ms": ts,
                "expiration_at_ms": int((time.time() + days * 86400) * 1000)}
    assert await entitlements.apply_event(db, ev("RENEWAL", 2000, 30))
    assert not await entitlements.apply_event(db, ev("INITIAL_PURCHASE", 1000, 1))
    doc = await db.entitlements.find_one({"user_id": "w1"})
    assert doc["expires_at"].timestamp() > time.time() + 20 * 86400

    # per-IP OTP cap
    auth_router._otp_hits.clear()
    assert not any(auth_router._ip_limited(Req()) for _ in range(10))
    assert auth_router._ip_limited(Req())

    print("ok")


asyncio.run(main())
