"""Account controls: email me my data, delete my account."""
import csv
import io
import json
import logging
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from services import minio_client
from services.email_service import send_export

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/account", tags=["account"])

# Everything keyed by user_id that belongs to the user. barcode_products is a shared cache, so it stays.
_USER_COLLECTIONS = (
    "user_profile", "food_logs", "saved_foods", "saved_meals", "supplements", "supplement_logs",
    "sleep_logs", "weight_photos", "body_photos", "body_photo_comparisons", "gym_sessions",
    "if_logs", "daily_checkins", "ai_usage", "food_corrections", "weekly_insights",
    "scan_results", "goal_history", "entitlements",
)
# Collections whose docs hold uploaded image URLs (image_url, or photo_url on the profile).
_IMAGE_COLLECTIONS = (
    "user_profile", "food_logs", "saved_foods", "saved_meals", "weight_photos", "body_photos", "gym_sessions", "scan_results",
)
# Collections included in the emailed export (the data the user typed or scanned in).
_EXPORT_COLLECTIONS = (
    "user_profile", "food_logs", "saved_foods", "saved_meals", "supplements", "supplement_logs",
    "sleep_logs", "weight_photos", "daily_checkins", "goal_history",
)
_EXPORT_COOLDOWN = timedelta(hours=1)


class DeleteAccount(BaseModel):
    email: str  # must match the signed-in account, a typed confirmation


def _clean(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if k in ("fcm_token",):
            continue
        out[k] = str(v) if isinstance(v, ObjectId) else v.isoformat() if isinstance(v, datetime) else v
    return out


def _food_csv(logs: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "meal", "item", "weight_g", "kcal", "protein_g", "carbs_g", "fat_g"])
    for log in sorted(logs, key=lambda d: (d.get("date", ""), str(d.get("created_at", "")))):
        for it in log.get("items") or []:
            w.writerow([
                log.get("date"), log.get("meal_slot"), it.get("name"),
                it.get("estimated_weight_g"), it.get("calories_kcal"),
                it.get("protein_g"), it.get("carbs_g"), it.get("fat_g"),
            ])
    return buf.getvalue().encode("utf-8")


@router.post("/export-email")
async def export_email(user_id: str = Depends(get_current_user)):
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user or not user.get("email"):
        raise HTTPException(404, "User not found")

    last = user.get("last_export_at")
    if last:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        wait = last + _EXPORT_COOLDOWN - datetime.now(timezone.utc)
        if wait.total_seconds() > 0:
            raise HTTPException(429, f"You can request another export in {int(wait.total_seconds() // 60) + 1} min")

    data: dict[str, list] = {}
    for name in _EXPORT_COLLECTIONS:
        docs = await db[name].find({"user_id": user_id}).to_list(None)
        data[name] = [_clean(d) for d in docs]

    files = {
        "bhojan_data.json": json.dumps(data, indent=2, default=str).encode("utf-8"),
        "food_logs.csv": _food_csv(data["food_logs"]),
    }
    await send_export(user["email"], files)
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"last_export_at": datetime.now(timezone.utc)}})
    return {"sent_to": user["email"]}


@router.delete("", status_code=204)
async def delete_account(body: DeleteAccount, user_id: str = Depends(get_current_user)):
    """Permanently remove the account and everything logged under it."""
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")
    if body.email.strip().lower() != (user.get("email") or "").lower():
        raise HTTPException(400, "Email doesn't match this account")

    # Collect uploaded image URLs before the docs go. Extra "image_url" holders (saved foods, scans) point at the same files.
    urls = set()
    for name in _IMAGE_COLLECTIONS:
        async for d in db[name].find({"user_id": user_id}, {"image_url": 1, "photo_url": 1}):
            urls.update(u for u in (d.get("image_url"), d.get("photo_url")) if u)
    for name in _USER_COLLECTIONS:
        await db[name].delete_many({"user_id": user_id})
    await db.otp_requests.delete_many({"email": user["email"]})
    await db.users.delete_one({"_id": user["_id"]})
    for url in urls:
        await minio_client.delete_image(url)
    logger.info("Account %s deleted", user_id)
