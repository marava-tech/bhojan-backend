import asyncio, io, zipfile, sys
sys.path.insert(0, ".")
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from fastapi import HTTPException
from services import data_export as de

class Cur:
    def __init__(s, rows): s.rows = rows
    async def to_list(s, _): return list(s.rows)
class Coll:
    def __init__(s): s.rows = []
    def find(s, q=None, *a, **k): return Cur([r for r in s.rows if all(r.get(k) == v for k, v in (q or {}).items() if not isinstance(v, dict))])
    async def find_one(s, q, sort=None):
        rs = [r for r in s.rows if all(r.get(k) == v for k, v in q.items())]
        return max(rs, key=lambda r: r.get("requested_at", 0)) if rs else None
    async def insert_one(s, d): s.rows.append(d)
class DB(dict):
    def __getattr__(s, k): return s[k]
    def __missing__(s, k): s[k] = Coll(); return s[k]

async def main():
    db = DB(); uid = str(ObjectId())
    db.users.rows.append({"_id": ObjectId(uid), "email": "a@b.c"})
    db.food_logs.rows.append({"user_id": uid, "date": "2026-01-01", "meal_slot": "lunch", "items": [{"name": "Dal", "calories_kcal": 120}], "image_url": None})
    db.sleep_logs.rows.append({"user_id": uid, "date": "2026-01-01", "hours_slept": 7.5})
    out = await de.request_export(db, uid); assert out["status"] == "queued", out
    for _ in range(2):
        try: await de.request_export(db, uid); assert False
        except HTTPException as e: assert e.status_code == 409
    run = db.data_exports.rows[0]["run_at"]; req = db.data_exports.rows[0]["requested_at"]
    assert timedelta(hours=2) <= run - req <= timedelta(hours=6), run - req
    db.data_exports.rows[0]["status"] = "sent"
    try: await de.request_export(db, uid); assert False
    except HTTPException as e: assert e.status_code == 429
    db.data_exports.rows[0]["requested_at"] -= timedelta(days=8)
    assert (await de.request_export(db, uid))["status"] == "queued"
    z = zipfile.ZipFile(io.BytesIO(await de.build_zip(db, uid, "a@b.c")))
    names = z.namelist(); print(names)
    assert "food_log_items.csv" in names and "sleep_logs.csv" in names and "Dal" in z.read("food_log_items.csv").decode()
    print("ok")
asyncio.run(main())
