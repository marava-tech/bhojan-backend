"""Self-check for services/ai_quota.py. Needs a Mongo: MONGODB_URI=mongodb://127.0.0.1:27099 python -m scripts.check_ai_quota"""
import asyncio
import os

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("AI_FREE_SCANS_PER_DAY", "3")

from fastapi import HTTPException  # noqa: E402

from database import get_db  # noqa: E402
from services import ai_quota  # noqa: E402


async def main():
    db = get_db()
    for c in ("ai_usage", "user_profile", "entitlements"):
        await db[c].delete_many({})
    u, profile = "u1", {"user_timezone": "Asia/Kolkata"}

    for _ in range(3):
        await ai_quota.consume(db, u, profile)
    try:
        await ai_quota.consume(db, u, profile)
        raise AssertionError("4th scan should be blocked")
    except HTTPException as e:
        assert e.status_code == 429 and e.detail["code"] == "ai_quota_exhausted", e.detail

    await ai_quota.refund(db, u, profile)
    await ai_quota.consume(db, u, profile)  # refunded slot is usable again

    # welcome boost: account < 7 days old gets 8/day, older gets 3
    from datetime import datetime, timedelta, timezone
    new = {"created_at": datetime.now(timezone.utc) - timedelta(days=2)}
    old = {"created_at": datetime.now(timezone.utc) - timedelta(days=30)}
    assert (await ai_quota.status(db, "n1", new))["limit"] == 8
    assert (await ai_quota.status(db, "n2", old))["limit"] == 3

    # gate: handler raising refunds; retry-with-model is free
    async def run_gate(gate_gen, fail):
        g = gate_gen
        await g.__anext__()
        if fail:
            try:
                await g.athrow(RuntimeError("boom"))
            except RuntimeError:
                pass
        else:
            try:
                await g.__anext__()
            except StopAsyncIteration:
                pass

    await run_gate(ai_quota.scan_gate("u3"), fail=True)
    assert (await ai_quota.status(db, "u3", None))["used"] == 0, "failed scan must be refunded"
    await run_gate(ai_quota.photo_scan_gate(model="gpt-4o", user_id="u3"), fail=False)
    assert (await ai_quota.status(db, "u3", None))["used"] == 0, "retry must be free"
    await run_gate(ai_quota.photo_scan_gate(model=None, user_id="u3"), fail=False)
    assert (await ai_quota.status(db, "u3", None))["used"] == 1
    print("ai_quota OK")


asyncio.run(main())
