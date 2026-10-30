"""Offline check of services/limits.py with a fake db. Run: python scripts/check_limits.py"""
import asyncio, sys, types
sys.path.insert(0, ".")
for m in ("auth", "database"):  # limits imports these only for FastAPI wiring
    sys.modules[m] = types.SimpleNamespace(get_current_user=lambda: None, get_db=lambda: None)
from services import ai_quota, limits
from fastapi import HTTPException

class Res:
    def __init__(s, n): s.modified_count = n
class Coll:
    def __init__(s): s.rows = []
    def _m(s, q): return [r for r in s.rows if all((r.get(k) == v) if not isinstance(v, dict) else
        all((r.get(k, 0) < x) if op == "$lt" else (r.get(k, 0) > x) if op == "$gt" else (r.get(k) is not None) if op == "$ne" else True
            for op, x in v.items()) for k, v in q.items())]
    async def update_one(s, q, u, upsert=False):
        rows = s._m(q)
        if not rows and upsert:
            d = {k: v for k, v in q.items() if not isinstance(v, dict)}; d.update(u.get("$setOnInsert", {})); s.rows.append(d); return Res(0)
        for r in rows[:1]:
            for k, v in u.get("$inc", {}).items(): r[k] = r.get(k, 0) + v
        return Res(len(rows[:1]))
    async def find_one(s, q): return (s._m(q) or [None])[0]
    async def count_documents(s, q): return len(s._m(q))
class DB(dict):
    def __missing__(s, k): s[k] = Coll(); return s[k]
    def __getattr__(s, k): return s[k]

async def main():
    async def prem(*_): return False
    ai_quota._premium = prem
    db = DB()
    await limits.consume(db, "u", None, "photo_compare"); await limits.consume(db, "u", None, "photo_compare")
    try: await limits.consume(db, "u", None, "photo_compare"); assert False
    except HTTPException as e: assert e.status_code == 429 and e.detail["code"] == "limit_reached", e.detail
    await limits.refund(db, "u", None, "photo_compare"); await limits.consume(db, "u", None, "photo_compare")
    async def prem2(*_): return True
    ai_quota._premium = prem2
    for _ in range(15 - 2 + 0): await limits.consume(db, "p", None, "photo_compare")
    for _ in range(2): await limits.consume(db, "p", None, "photo_compare")
    try: await limits.consume(db, "p", None, "photo_compare"); assert False
    except HTTPException as e: assert e.detail["limit"] == 15 and e.detail["premium"]
    for i in range(20): db.body_photos.rows.append({"user_id": "f", "image_url": "x", "created_at": limits.datetime.now(limits.timezone.utc)})
    ai_quota._premium = prem
    try: await limits.check_photo_upload(db, "f", None); assert False
    except HTTPException as e: assert e.detail["limit"] == 20
    db.weight_photos.rows.append({"user_id": "w", "image_url": None})  # weight-only doesn't count
    await limits.check_photo_upload(db, "w", None)
    print("ok")
asyncio.run(main())
