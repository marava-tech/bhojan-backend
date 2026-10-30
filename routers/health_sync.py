"""Health Connect / Google Health sync state — tracks when the client's
manual "sync missed data" backfill last completed, so the next run only
covers the gap since then instead of re-scanning the user's full history.
Also receives daily total energy burned (WHOOP, Fitbit, Galaxy Watch… via
Health Connect), which the adaptive TDEE falls back to."""
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/api/health-sync", tags=["health-sync"])


@router.get("/last-synced")
async def get_last_synced(user_id: str = Depends(get_current_user)):
    db = get_db()
    doc = await db.user_profile.find_one({"user_id": user_id}, {"health_last_synced_at": 1})
    if not doc:
        raise HTTPException(404, "Profile not found")
    last_synced = doc.get("health_last_synced_at")
    return {"last_synced_at": last_synced.isoformat() if last_synced else None}


@router.put("/last-synced")
async def set_last_synced(user_id: str = Depends(get_current_user)):
    db = get_db()
    now = datetime.now(timezone.utc)
    result = await db.user_profile.update_one(
        {"user_id": user_id}, {"$set": {"health_last_synced_at": now}}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Profile not found")
    return {"last_synced_at": now.isoformat()}


class EnergyDay(BaseModel):
    date: date
    kcal: float = Field(ge=800, le=8000)  # a full day's total burn; partial/garbage days are rejected


class EnergyUpload(BaseModel):
    days: list[EnergyDay] = Field(max_length=60)
    source: str = Field(default="health_connect", max_length=100)


@router.put("/energy")
async def put_energy(body: EnergyUpload, user_id: str = Depends(get_current_user)):
    """Upsert one total-energy-burned value per day; re-sending a day overwrites it."""
    db = get_db()
    now = datetime.now(timezone.utc)
    for d in body.days:
        await db.energy_burned.update_one(
            {"user_id": user_id, "date": d.date.isoformat()},
            {"$set": {"kcal": round(d.kcal), "source": body.source, "updated_at": now}},
            upsert=True,
        )
    return {"saved": len(body.days)}
