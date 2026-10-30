"""Run: JWT_SECRET=x python3 scripts/check_otp_limit.py — wrong-code cap, hashing, and has_profile on login."""
import asyncio, os, sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:1/x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import HTTPException
from routers import auth_router as a
from models.user import SendOtpRequest, VerifyOtpRequest


class Coll:
    def __init__(self): self.docs = []
    async def find_one(self, q, proj=None): return next((d for d in self.docs if all(d.get(k) == v for k, v in q.items())), None)
    async def find_one_and_update(self, q, upd, return_document=None):
        d = await self.find_one(q)
        if d:
            for k, v in upd["$inc"].items(): d[k] = d.get(k, 0) + v
        return d
    async def delete_many(self, q): self.docs = [d for d in self.docs if any(d.get(k) != v for k, v in q.items())]
    async def insert_one(self, d):
        d["_id"] = len(self.docs) + 1; self.docs.append(d); return SimpleNamespace(inserted_id=d["_id"])


db = SimpleNamespace(otp_requests=Coll(), users=Coll(), user_profile=Coll())
a.get_db = lambda: db


def seed(otp="123456"):
    db.otp_requests.docs = [{"email": "u@x.com", "otp_hash": a._hash_otp("u@x.com", otp), "attempts": 0,
                             "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}]


async def verify(otp):
    try: return await a.verify_otp(VerifyOtpRequest(email="u@x.com", otp=otp))
    except HTTPException as e: return e.status_code


async def main():
    seed()
    assert "123456" not in str(db.otp_requests.docs)  # stored hashed
    for _ in range(5): assert await verify("000001") == 400
    assert await verify("123456") == 429, "6th guess must be refused even if correct"
    assert "otp_hash" not in db.otp_requests.docs[0], "code is burned after the cap"
    assert await verify("123456") == 429, "still locked"
    try: await a.send_otp(SendOtpRequest(email="u@x.com")); assert False, "send-otp must be locked too"
    except HTTPException as e: assert e.status_code == 429
    db.otp_requests.docs[0]["locked_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await verify("123456") == 400, "lockout over: no live code, not 429"
    seed(); t = await verify("123456")
    assert t.is_new_user and not t.has_profile
    seed(); await db.user_profile.insert_one({"user_id": "1"}); t = await verify("123456")
    assert not t.is_new_user and t.has_profile
    print("ok")

asyncio.run(main())
