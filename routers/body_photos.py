"""Standalone body photo uploads — decoupled from gym sessions."""
import json
import logging
import uuid
from datetime import datetime, timezone, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from enum import Enum

from auth import get_current_user
from database import get_db
from services import gemini as gemini_svc
from services import minio_client
from services import entitlements
from utils import validate_image_upload, get_openrouter_key, require_feature_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/body-photos", tags=["body-photos"])


class BodyPhotoAngle(str, Enum):
    front = "front"
    back = "back"
    side = "side"


@router.post("", status_code=201)
async def upload_body_photo(
    angle: BodyPhotoAngle = Form(...),
    photo_date: str = Form(...),  # YYYY-MM-DD
    photo: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    require_feature_enabled(profile, "progress_photos", "Progress photos")

    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)

    filename = f"{uuid.uuid4()}.jpg"
    image_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_GYM, filename)

    photo_id = str(uuid.uuid4())
    doc = {
        "photo_id": photo_id,
        "date": photo_date,
        "angle": angle.value,
        "image_url": image_url,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
    }
    await db.body_photos.insert_one(doc)
    doc["_id"] = str(doc.pop("_id", photo_id))
    return doc


class CompareRequest(BaseModel):
    photo_ids: list[str]


@router.post("/compare")
async def compare_body_photos(
    req: CompareRequest,
    user_id: str = Depends(get_current_user),
    _premium: None = Depends(entitlements.require_premium),
):
    """Compare 2–3 body photos using AI visual progression analysis."""
    if len(req.photo_ids) < 2 or len(req.photo_ids) > 3:
        raise HTTPException(status_code=400, detail="Select 2 or 3 photos to compare")

    db = get_db()

    # Fetch user's API key (falls back to server-wide key for internal testing)
    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    docs = []
    for pid in req.photo_ids:
        doc = await db.body_photos.find_one({"photo_id": pid, "user_id": user_id})
        if not doc:
            raise HTTPException(status_code=404, detail=f"Photo {pid} not found")
        docs.append(doc)

    angles = {d["angle"] for d in docs}
    if len(angles) > 1:
        raise HTTPException(status_code=400, detail="All selected photos must be the same angle")

    photos_payload = []
    for doc in docs:
        image_bytes = await minio_client.download_image(doc["image_url"])
        photos_payload.append({
            "image_bytes": image_bytes,
            "date": doc["date"],
            "angle": doc["angle"],
            "photo_id": doc["photo_id"],
            "image_url": doc["image_url"],
        })

    try:
        comparison = await gemini_svc.compare_body_photos(photos_payload, api_key=api_key)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail=f"AI comparison failed: {e}")

    photos_meta = [
        {"photo_id": d["photo_id"], "date": d["date"], "angle": d["angle"], "image_url": d["image_url"]}
        for d in sorted(docs, key=lambda x: x["date"])
    ]
    comparison_id = str(uuid.uuid4())
    await db.body_photo_comparisons.insert_one({
        "comparison_id": comparison_id,
        "angle": next(iter(angles)),
        "photos": photos_meta,
        "comparison": comparison,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
    })

    return {
        "comparison_id": comparison_id,
        "photos": photos_meta,
        "comparison": comparison,
    }


@router.get("/pair")
async def photo_pair(a: str, b: str, user_id: str = Depends(get_current_user)):
    """Two of the user's photos with their dates, the days between them, and the weight near each date,
    so the compare screen can say "-3.2 kg in 48 days"."""
    db = get_db()
    photos = {}
    for key, pid in (("a", a), ("b", b)):
        doc = await db.body_photos.find_one({"photo_id": pid, "user_id": user_id}, {"_id": 0, "user_id": 0})
        if not doc:
            raise HTTPException(404, "Photo not found")
        photos[key] = doc

    weights = await db.weight_photos.find({"user_id": user_id, "weight_kg": {"$ne": None}}, {"date": 1, "weight_kg": 1}).to_list(None)

    def weight_near(day: str) -> float | None:
        # ponytail: nearest weigh-in within a week of the photo; use the trend line if daily noise matters.
        d = date.fromisoformat(day)
        best = min(weights, key=lambda w: abs((date.fromisoformat(w["date"]) - d).days), default=None)
        if best and abs((date.fromisoformat(best["date"]) - d).days) <= 7:
            return float(best["weight_kg"])
        return None

    for p in photos.values():
        p["weight_kg"] = weight_near(p["date"])
    wa, wb = photos["a"]["weight_kg"], photos["b"]["weight_kg"]
    days = abs((date.fromisoformat(photos["b"]["date"]) - date.fromisoformat(photos["a"]["date"])).days)
    return {
        "a": photos["a"],
        "b": photos["b"],
        "days_apart": days,
        # change from the earlier photo to the later one
        "weight_change_kg": None if wa is None or wb is None else round((wb - wa) if photos["b"]["date"] >= photos["a"]["date"] else (wa - wb), 1),
    }


@router.get("/comparisons")
async def list_comparisons(user_id: str = Depends(get_current_user)):
    db = get_db()
    docs = await db.body_photo_comparisons.find({"user_id": user_id}).sort("created_at", -1).to_list(None)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"comparisons": docs}


@router.get("")
async def list_body_photos(days: int = 0, limit: int = 0, user_id: str = Depends(get_current_user)):
    """Newest first. days/limit 0 = no cap — a 90-day default hid most of a long-time user's photos."""
    db = get_db()
    query: dict = {"user_id": user_id}
    if days > 0:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        query["date"] = {"$gte": cutoff}
    docs = await db.body_photos.find(query).sort("date", -1).limit(max(limit, 0)).to_list(None)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"photos": docs}


@router.delete("/{photo_id}", status_code=204)
async def delete_body_photo(photo_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    result = await db.body_photos.delete_one({"photo_id": photo_id, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Body photo not found")

