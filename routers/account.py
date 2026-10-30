"""Account controls: export my data, delete my account."""
import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from services import minio_client
from services import data_export

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/account", tags=["account"])

# Everything keyed by user_id that belongs to the user. barcode_products is a shared cache, so it stays.
_USER_COLLECTIONS = (
    "user_profile", "food_logs", "saved_foods", "saved_meals", "supplements", "supplement_logs",
    "sleep_logs", "weight_photos", "body_photos", "body_photo_comparisons", "gym_sessions",
    "if_logs", "daily_checkins", "ai_usage", "food_corrections", "weekly_insights",
    "scan_results", "goal_history", "entitlements", "data_exports",
)
# Collections whose docs hold uploaded image URLs (image_url, or photo_url on the profile).
_IMAGE_COLLECTIONS = (
    "user_profile", "food_logs", "saved_foods", "saved_meals", "weight_photos", "body_photos", "gym_sessions", "scan_results",
)


class DeleteAccount(BaseModel):
    email: str  # must match the signed-in account, a typed confirmation


@router.post("/export", status_code=202)
async def request_export(user_id: str = Depends(get_current_user)):
    """Queue a ZIP of the user's data. Emailed 2-6 hours later; one request a week."""
    return await data_export.request_export(get_db(), user_id)


@router.get("/export")
async def export_status(user_id: str = Depends(get_current_user)):
    return await data_export.latest(get_db(), user_id)


@router.post("/export-email", status_code=202)
async def export_email(user_id: str = Depends(get_current_user)):
    """Old app versions; same queued export."""
    out = await data_export.request_export(get_db(), user_id)
    return {"sent_to": out["email"], **out}


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
