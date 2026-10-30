"""Data export: a ZIP of everything the user logged, built by a background job and emailed.

Why a queued job, not an instant download (Google Takeout, Spotify, Instagram and Strava all work this way):
the archive reads a dozen collections and downloads photos, which is too heavy to do on the request path.
The request is accepted and scheduled 2-6 hours out, so the work is batched away from peak time and a
hijacked session can't pull a full copy of someone's data instantly.

Limits: one export per 7 days, one in flight at a time, 3 build attempts, photos capped so the ZIP always fits
as an email attachment (anything over budget is listed with its URL in photos_index.csv instead).
"""
import csv
import io
import json
import logging
import os
import random
import tempfile
import zipfile
from datetime import datetime, timezone, timedelta

import httpx
from bson import ObjectId
from fastapi import HTTPException

from services.email_service import send_export

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(days=7)
DELAY_HOURS = (float(os.environ.get("EXPORT_MIN_DELAY_H", "2")), float(os.environ.get("EXPORT_MAX_DELAY_H", "6")))
MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=30)       # a "processing" job older than this died with its worker
ZIP_BUDGET = 15 * 1024 * 1024             # Gmail caps a message at 25 MB and base64 adds ~33%
MAX_PHOTOS = 300

# What the user typed, scanned or logged. Caches and usage counters are left out.
COLLECTIONS = (
    "user_profile", "food_logs", "saved_foods", "saved_meals", "supplements", "supplement_logs",
    "sleep_logs", "weight_photos", "body_photos", "daily_checkins", "goal_history", "gym_sessions", "if_logs",
)
PHOTO_COLLECTIONS = ("weight_photos", "body_photos", "food_logs")

_README = """Your Bhojan data
================
Exported {when} UTC for {email}.

data.json            Everything below in one file (the complete, lossless copy).
food_log_items.csv   Every food you logged, one row per item, with calories and macros.
<name>.csv           One spreadsheet per section: profile, saved foods and meals, supplements, sleep,
                     weight, check-ins and goal history.
photos/              Your progress and meal photos. If there were too many to fit in an email, the rest
                     are listed with a link in photos_index.csv.

Calories in your goals are protein x 4 + carbs x 4 + fat x 9.
"""


def _utc(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


def _clean(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if k == "fcm_token":
            continue
        out[k] = str(v) if isinstance(v, ObjectId) else v.isoformat() if isinstance(v, datetime) else v
    return out


def _csv(rows: list[dict]) -> bytes:
    """Flat spreadsheet of any list of docs: one column per key, nested values as JSON."""
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in (r.get(c) for c in cols)])
    return buf.getvalue().encode("utf-8")


def _food_items_csv(logs: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "meal", "item", "weight_g", "kcal", "protein_g", "carbs_g", "fat_g"])
    for log in sorted(logs, key=lambda d: (d.get("date", ""), str(d.get("created_at", "")))):
        for it in log.get("items") or []:
            w.writerow([
                log.get("date"), log.get("meal_slot"), it.get("name"), it.get("estimated_weight_g"),
                it.get("calories_kcal"), it.get("protein_g"), it.get("carbs_g"), it.get("fat_g"),
            ])
    return buf.getvalue().encode("utf-8")


async def build_zip(db, user_id: str, email: str) -> bytes:
    data = {}
    for name in COLLECTIONS:
        data[name] = [_clean(d) for d in await db[name].find({"user_id": user_id}).to_list(None)]

    index = [["collection", "date", "url", "included_as"]]
    with tempfile.TemporaryFile() as tmp:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", _README.format(when=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), email=email))
            z.writestr("data.json", json.dumps(data, indent=2, default=str))
            z.writestr("food_log_items.csv", _food_items_csv(data["food_logs"]))
            for name, rows in data.items():
                if rows and name != "food_logs":
                    z.writestr(f"{name}.csv", _csv(rows))

            # Photos, newest first, until the archive would no longer fit in an email.
            photos = sorted(
                ((c, d.get("date") or d.get("photo_date") or "", d["image_url"])
                 for c in PHOTO_COLLECTIONS for d in data[c] if d.get("image_url")),
                key=lambda p: p[1], reverse=True,
            )
            async with httpx.AsyncClient(timeout=20.0) as http:
                for n, (coll, date, url) in enumerate(photos):
                    path = None
                    if n < MAX_PHOTOS and tmp.tell() < ZIP_BUDGET * 0.9:
                        try:
                            r = await http.get(url)
                            r.raise_for_status()
                            if tmp.tell() + len(r.content) < ZIP_BUDGET:
                                path = f"photos/{coll}/{date}_{n}{os.path.splitext(url.split('?')[0])[1] or '.jpg'}"
                                z.writestr(path, r.content)  # already compressed, deflate gains nothing
                        except Exception as e:
                            logger.warning("Export photo skipped (%s): %s", url, e)
                    index.append([coll, date, url, path or "not included, use the link"])
            buf = io.StringIO()
            csv.writer(buf).writerows(index)
            z.writestr("photos_index.csv", buf.getvalue())
        tmp.seek(0)
        return tmp.read()


def _view(doc: dict | None) -> dict:
    """What the app needs: current state and when the next request is allowed."""
    if not doc:
        return {"status": "none", "next_allowed_at": None}
    requested = _utc(doc["requested_at"])
    return {
        "status": doc["status"],
        "requested_at": requested.isoformat(),
        "email": doc["email"],
        "sent_at": _utc(doc.get("sent_at")).isoformat() if doc.get("sent_at") else None,
        # A failed export doesn't use up the week.
        "next_allowed_at": None if doc["status"] == "failed" else (requested + COOLDOWN).isoformat(),
    }


async def latest(db, user_id: str) -> dict:
    return _view(await db.data_exports.find_one({"user_id": user_id}, sort=[("requested_at", -1)]))


async def request_export(db, user_id: str) -> dict:
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user or not user.get("email"):
        raise HTTPException(404, "User not found")
    last = await db.data_exports.find_one({"user_id": user_id}, sort=[("requested_at", -1)])
    if last and last["status"] != "failed":
        if last["status"] in ("queued", "processing"):
            raise HTTPException(409, "Your export is already on its way. We'll email it as soon as it's ready.")
        wait = _utc(last["requested_at"]) + COOLDOWN - datetime.now(timezone.utc)
        if wait.total_seconds() > 0:
            raise HTTPException(429, f"You can request one export a week. Next one: in {wait.days + 1} day(s).")
    now = datetime.now(timezone.utc)
    await db.data_exports.insert_one({
        "user_id": user_id, "email": user["email"], "status": "queued", "attempts": 0,
        "requested_at": now, "run_at": now + timedelta(hours=random.uniform(*DELAY_HOURS)),
    })
    return await latest(db, user_id)


async def process_due(db) -> None:
    """Scheduler tick: build and send every export whose time has come, one at a time."""
    now = datetime.now(timezone.utc)
    await db.data_exports.update_many(
        {"status": "processing", "started_at": {"$lt": now - STALE_AFTER}}, {"$set": {"status": "queued"}}
    )
    while True:
        # Atomic claim, so a second worker can't pick up the same job.
        job = await db.data_exports.find_one_and_update(
            {"status": "queued", "run_at": {"$lte": datetime.now(timezone.utc)}},
            {"$set": {"status": "processing", "started_at": datetime.now(timezone.utc)}, "$inc": {"attempts": 1}},
            sort=[("run_at", 1)],
        )
        if not job:
            return
        try:
            archive = await build_zip(db, job["user_id"], job["email"])
            await send_export(job["email"], archive, f"bhojan-data-{datetime.now(timezone.utc):%Y-%m-%d}.zip")
            await db.data_exports.update_one(
                {"_id": job["_id"]}, {"$set": {"status": "sent", "sent_at": datetime.now(timezone.utc), "bytes": len(archive)}}
            )
            logger.info("Export sent for user %s (%d KB)", job["user_id"], len(archive) // 1024)
        except Exception as e:
            logger.exception("Export failed for user %s", job["user_id"])
            retry = job["attempts"] + 1 <= MAX_ATTEMPTS
            await db.data_exports.update_one({"_id": job["_id"]}, {"$set": {
                "status": "queued" if retry else "failed", "error": str(e)[:300],
                "run_at": datetime.now(timezone.utc) + timedelta(minutes=15),
            }})
