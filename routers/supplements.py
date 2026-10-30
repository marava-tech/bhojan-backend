"""Supplement tracker — presets + daily logs + streaks + correlation."""
import logging
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from bson import ObjectId
from typing import Optional

from auth import get_current_user
from database import get_db
from models.supplement import SupplementCreate, SupplementPatch, SupplementLogCreate, SupplementLogSet
from services import ai_quota
from services import gemini as gemini_svc
from services.supplement_presets import PRESETS
from services.streak_calc import consecutive_days
from utils import get_openrouter_key, parse_object_id, validate_image_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/supplements", tags=["supplements"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


# ─── Supplement presets ──────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_supplement(body: SupplementCreate, user_id: str = Depends(get_current_user)):
    db = get_db()
    doc = {
        **body.model_dump(),
        "user_id": user_id,
        "current_streak": 0,
        "best_streak": 0,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.supplements.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc


@router.get("")
async def list_supplements(
    skip: int = 0, limit: int = 50, user_id: str = Depends(get_current_user)
):
    db = get_db()
    docs = await db.supplements.find({"user_id": user_id}).skip(skip).limit(limit).to_list(None)
    return [_serialize(d) for d in docs]


@router.patch("/{supp_id}")
async def update_supplement(
    supp_id: str, body: SupplementPatch, user_id: str = Depends(get_current_user)
):
    db = get_db()
    oid = parse_object_id(supp_id, "supp_id")
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(400, "No fields provided")
    result = await db.supplements.update_one({"_id": oid, "user_id": user_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(404, "Supplement not found")
    doc = await db.supplements.find_one({"_id": oid, "user_id": user_id})
    return _serialize(doc)


@router.delete("/{supp_id}", status_code=204)
async def delete_supplement(supp_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    oid = parse_object_id(supp_id, "supp_id")
    result = await db.supplements.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Supplement not found")


@router.get("/presets")
async def list_presets():
    """Popular supplements with a typical serving and macros, for one-tap adding."""
    return PRESETS


@router.post("/read-label")
async def read_supplement_label(
    photo: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    _quota: None = Depends(ai_quota.scan_gate),
):
    """Read a supplement tub's nutrition label into the add-supplement form: one serving's size and macros."""
    db = get_db()
    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)
    api_key = get_openrouter_key(await db.user_profile.find_one({"user_id": user_id}))
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    data = await gemini_svc.read_nutrition_label(image_bytes, api_key=api_key)
    per = data.get("per_100g") or {}
    try:
        per = {k: float(per[k]) for k in ("calories_kcal", "protein_g", "carbs_g", "fat_g")}
    except (KeyError, TypeError, ValueError):
        per = None
    # 0 kcal is fine here (creatine, vitamins); negative or absurd values mean a misread.
    if data.get("error") or not per or not (0 <= per["calories_kcal"] <= 950) or min(per.values()) < 0:
        raise HTTPException(422, "Couldn't read that label. Try a closer, sharper photo of the nutrition table.")

    serving = data.get("serving_g")
    serving = float(serving) if isinstance(serving, (int, float)) and serving > 0 else 100.0
    scale = serving / 100
    return {
        "name": data.get("name"),
        "dose_amount": round(serving, 1),
        "dose_unit": "g",
        "calories_per_unit": round(per["calories_kcal"] * scale, 1),
        "protein_per_unit": round(per["protein_g"] * scale, 1),
        "carbs_per_unit": round(per["carbs_g"] * scale, 1),
        "fat_per_unit": round(per["fat_g"] * scale, 1),
    }


# ─── Supplement logs ─────────────────────────────────────────────────────────

_MACRO_KEYS = (("calories_kcal", "calories_per_unit"), ("protein_g", "protein_per_unit"),
               ("carbs_g", "carbs_per_unit"), ("fat_g", "fat_per_unit"))


async def _rebuild_day(db, user_id: str, date_str: str) -> dict:
    """Make the day's derived records match its supplement logs: one 'supplement' food entry (so calories and
    protein from e.g. whey count toward the day) and the daily check-in's supplement list (what history reads).
    Derived from the logs every time, so it can't drift however many times a chip is tapped."""
    logs = await db.supplement_logs.find({"date": date_str, "user_id": user_id}).to_list(None)
    supps = {str(s["_id"]): s for s in await db.supplements.find({"user_id": user_id}).to_list(None)}

    entries, items = [], []
    totals = {k: 0.0 for k, _ in _MACRO_KEYS}
    for log in logs:
        s = supps.get(log["supplement_id"])
        if not s:
            continue
        units = log.get("units_taken") or 1
        entries.append({"supplement_id": log["supplement_id"], "name": s["name"], "taken": True,
                        "quantity": units, "unit": s.get("dose_unit", "")})
        macros = {k: round((s.get(per) or 0) * units, 1) for k, per in _MACRO_KEYS}
        if any(macros.values()):
            weight = (s.get("dose_amount") or 0) * units if s.get("dose_unit") == "g" else 0
            items.append({"name": s["name"], "estimated_weight_g": float(weight), **macros, "macro_source": "database"})
            for k in totals:
                totals[k] += macros[k]

    now = datetime.now(timezone.utc)
    await db.food_logs.delete_many({"date": date_str, "meal_slot": "supplement", "user_id": user_id})
    if items:
        await db.food_logs.insert_one({
            "user_id": user_id, "date": date_str, "meal_slot": "supplement", "source_type": None,
            "items": items, "image_url": None, "note": None, "ai_kcal": None, "created_at": now,
            "totals": {k: round(v, 1) for k, v in totals.items()},
        })
    await db.daily_checkins.update_one(
        {"date": date_str, "user_id": user_id},
        {"$set": {"supplement_entries": entries, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return {k: round(v, 1) for k, v in totals.items()}


@router.put("/logs")
async def set_supplement_taken(body: SupplementLogSet, user_id: str = Depends(get_current_user)):
    """Mark one supplement taken or not taken on a day. Idempotent: calling it twice changes nothing."""
    db = get_db()
    try:
        date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(400, "Invalid date, expected YYYY-MM-DD")
    supp = await db.supplements.find_one({"_id": parse_object_id(body.supplement_id, "supplement_id"), "user_id": user_id})
    if not supp:
        raise HTTPException(404, "Supplement not found")

    key = {"supplement_id": body.supplement_id, "date": body.date, "user_id": user_id}
    if body.taken and body.units_taken > 0:
        now = datetime.now(timezone.utc)
        await db.supplement_logs.update_one(
            key,
            {"$set": {"supplement_name": supp["name"], "units_taken": body.units_taken, "updated_at": now},
             "$setOnInsert": {**key, "created_at": now}},
            upsert=True,
        )
    else:
        await db.supplement_logs.delete_one(key)

    await _update_supplement_streak(body.supplement_id, user_id, db)
    totals = await _rebuild_day(db, user_id, body.date)
    return {"date": body.date, "supplement_id": body.supplement_id, "taken": body.taken and body.units_taken > 0,
            "supplement_totals": totals}


@router.post("/logs", status_code=201)
async def log_supplement(body: SupplementLogCreate, user_id: str = Depends(get_current_user)):
    db = get_db()
    supp = await db.supplements.find_one({"_id": parse_object_id(body.supplement_id, "supplement_id"), "user_id": user_id})
    if not supp:
        raise HTTPException(404, "Supplement not found")

    # Upsert: allow one log per supplement per date
    existing = await db.supplement_logs.find_one(
        {"supplement_id": body.supplement_id, "date": body.date, "user_id": user_id}
    )
    if existing:
        await db.supplement_logs.update_one(
            {"_id": existing["_id"]},
            {"$set": {"units_taken": body.units_taken, "updated_at": datetime.now(timezone.utc)}},
        )
        existing["_id"] = str(existing["_id"])
        await _update_supplement_streak(body.supplement_id, user_id, db)
        return existing

    doc = {
        "supplement_id": body.supplement_id,
        "supplement_name": supp["name"],
        "date": body.date,
        "units_taken": body.units_taken,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.supplement_logs.insert_one(doc)
    doc["_id"] = str(result.inserted_id)

    # Update streak for this supplement
    await _update_supplement_streak(body.supplement_id, user_id, db)

    return doc


@router.get("/logs")
async def get_supplement_logs(date_str: str, user_id: str = Depends(get_current_user)):
    """Returns checklist: all supplements with taken status for the date."""
    db = get_db()
    supplements = await db.supplements.find({"user_id": user_id}).to_list(None)
    logs = await db.supplement_logs.find({"date": date_str, "user_id": user_id}).to_list(None)
    logs_by_id = {l["supplement_id"]: l for l in logs}
    taken_ids = set(logs_by_id.keys())
    # Usual servings per supplement = the most recent log, so the app can start a new day at "what you took last time".
    last_units: dict[str, float] = {}
    recent = await db.supplement_logs.find({"user_id": user_id}).sort("date", -1).limit(300).to_list(None)
    for l in recent:
        last_units.setdefault(l["supplement_id"], l.get("units_taken") or 1)

    today_weekday = date.fromisoformat(date_str).weekday()  # 0=Mon

    checklist = []
    for s in supplements:
        sid = str(s["_id"])
        # Weekly supplements only shown on their day_of_week
        if s.get("frequency") == "weekly":
            if s.get("day_of_week") != today_weekday:
                continue
        log = logs_by_id.get(sid)
        checklist.append({
            "supplement_id": sid,
            "id": sid,
            "name": s["name"],
            "dose_amount": s["dose_amount"],
            "dose_unit": s["dose_unit"],
            "timing": s.get("timing"),
            "taken": sid in taken_ids,
            "units_taken": log["units_taken"] if log else 0,
            "last_units": last_units.get(sid, 1),
            "current_streak": s.get("current_streak", 0),
            "best_streak": s.get("best_streak", 0),
            "calories_per_unit": s.get("calories_per_unit", 0.0),
            "protein_per_unit": s.get("protein_per_unit", 0.0),
            "carbs_per_unit": s.get("carbs_per_unit", 0.0),
            "fat_per_unit": s.get("fat_per_unit", 0.0),
        })
    return {"date": date_str, "checklist": checklist}


async def _update_supplement_streak(supplement_id: str, user_id: str, db):
    logs = await db.supplement_logs.find(
        {"supplement_id": supplement_id, "user_id": user_id}, {"date": 1}
    ).to_list(None)
    current, best = await consecutive_days([l["date"] for l in logs])
    await db.supplements.update_one(
        {"_id": parse_object_id(supplement_id, "supplement_id"), "user_id": user_id},
        {"$set": {"current_streak": current, "best_streak": best}},
    )


# ─── Supplement-sleep correlation ────────────────────────────────────────────

@router.get("/{supp_id}/correlation")
async def supplement_correlation(
    supp_id: str,
    other: str = "sleep",
    user_id: str = Depends(get_current_user),
):
    """Compare sleep quality on supplement-taken nights vs not-taken nights."""
    db = get_db()

    supp = await db.supplements.find_one({"_id": parse_object_id(supp_id, "supp_id"), "user_id": user_id})
    if not supp:
        raise HTTPException(404, "Supplement not found")

    if other != "sleep":
        raise HTTPException(400, "Only 'sleep' correlation supported")

    supp_logs = await db.supplement_logs.find({"supplement_id": supp_id, "user_id": user_id}, {"date": 1}).to_list(None)
    supp_dates = {l["date"] for l in supp_logs}

    sleep_logs = await db.sleep_logs.find({"user_id": user_id}).to_list(None)

    QUALITY_SCORE = {"worst": 1, "bad": 2, "average": 3, "good": 4, "better": 5}

    taken_scores = []
    not_taken_scores = []
    for log in sleep_logs:
        score = QUALITY_SCORE.get(log.get("quality"), 0)
        if log["date"] in supp_dates:
            taken_scores.append(score)
        else:
            not_taken_scores.append(score)

    total = len(taken_scores) + len(not_taken_scores)
    if total < 14:
        return {
            "supplement_id": supp_id,
            "supplement_name": supp["name"],
            "status": "insufficient_data",
            "days_recorded": total,
            "required_days": 14,
        }

    avg_taken = round(sum(taken_scores) / len(taken_scores), 2) if taken_scores else 0
    avg_not_taken = round(sum(not_taken_scores) / len(not_taken_scores), 2) if not_taken_scores else 0

    return {
        "supplement_id": supp_id,
        "supplement_name": supp["name"],
        "status": "ready",
        "taken_days": len(taken_scores),
        "not_taken_days": len(not_taken_scores),
        "avg_sleep_quality_taken": avg_taken,
        "avg_sleep_quality_not_taken": avg_not_taken,
        "difference": round(avg_taken - avg_not_taken, 2),
        "insight": (
            f"{supp['name']} days: avg sleep {avg_taken}/5 vs {avg_not_taken}/5 without."
        ),
    }
