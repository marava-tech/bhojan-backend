"""One request for the Progress screen: weight (with trend), nutrition vs goal, sleep, and insights."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from services.progress import weight_trend, weekly_rate
from services.units import fmt_weight

router = APIRouter(prefix="/api/progress", tags=["progress"])

_MAX_DAYS = 365


@router.get("")
async def progress(days: int = 30, user_id: str = Depends(get_current_user)):
    """days: 7, 30, 90, or 0 for everything (capped at a year)."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id}) or {}
    try:
        tz = ZoneInfo(profile.get("user_timezone") or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()
    span = _MAX_DAYS if days <= 0 else min(days, _MAX_DAYS)
    start, end = (today - timedelta(days=span - 1)).isoformat(), today.isoformat()
    in_range = {"user_id": user_id, "date": {"$gte": start, "$lte": end}}

    # Weight: latest entry per day (the trend starts from the first weigh-in in range).
    w_docs = await db.weight_photos.find({**in_range, "weight_kg": {"$ne": None}}).sort([("date", 1), ("created_at", 1)]).to_list(None)
    per_day = {d["date"]: float(d["weight_kg"]) for d in w_docs}
    entries = sorted(per_day.items())
    trend = weight_trend(entries)
    rate = weekly_rate(trend)
    weight = None
    if entries:
        weight = {
            "entries": [{"date": d, "kg": round(kg, 2)} for d, kg in entries],
            "trend": [{"date": d, "kg": round(kg, 2)} for d, kg in trend],
            "trend_kg": round(trend[-1][1], 2),
            "change_kg": round(trend[-1][1] - trend[0][1], 2),
            "weekly_rate_kg": None if rate is None else round(rate, 2),
        }

    # Nutrition against the user's current goal.
    # ponytail: current goal for every day; use goal_history per day if past-goal accuracy matters.
    goal_kcal = profile.get("goal_kcal") or 0
    goal_protein = profile.get("protein_g") or 0
    n_rows = await db.food_logs.aggregate([
        {"$match": in_range},
        {"$group": {"_id": "$date", "kcal": {"$sum": "$totals.calories_kcal"}, "protein": {"$sum": "$totals.protein_g"}}},
        {"$sort": {"_id": 1}},
    ]).to_list(None)
    n_days = [{"date": r["_id"], "kcal": round(r["kcal"]), "protein_g": round(r["protein"], 1)} for r in n_rows if r["kcal"] > 0]
    on_target = sum(1 for d in n_days if goal_kcal and abs(d["kcal"] - goal_kcal) <= 0.1 * goal_kcal)
    protein_hits = sum(1 for d in n_days if goal_protein and d["protein_g"] >= 0.9 * goal_protein)
    nutrition = {
        "days": n_days,
        "goal_kcal": goal_kcal,
        "goal_protein_g": goal_protein,
        "logged_days": len(n_days),
        "avg_kcal": round(sum(d["kcal"] for d in n_days) / len(n_days)) if n_days else None,
        "days_on_calorie_target": on_target,
        "days_protein_hit": protein_hits,
    }

    s_docs = await db.sleep_logs.find({**in_range, "hours_slept": {"$ne": None}}).sort("date", 1).to_list(None)
    s_days = [{"date": d["date"], "hours": float(d["hours_slept"])} for d in s_docs]
    sleep = {
        "days": s_days,
        "avg_hours": round(sum(d["hours"] for d in s_days) / len(s_days), 1) if s_days else None,
        "days_in_range": sum(1 for d in s_days if 7 <= d["hours"] <= 9),
    }

    insights = []
    if rate is not None:
        direction = "down" if rate < -0.05 else "up" if rate > 0.05 else "steady"
        insights.append(f"Your weight trend is {direction}" + ("" if direction == "steady" else f" {fmt_weight(abs(rate), profile)} a week") + ".")
    elif len(entries) < 3:
        insights.append("Log 3 weigh-ins a week and your trend line appears here.")
    if n_days and goal_protein:
        insights.append(f"Protein goal hit on {protein_hits} of {len(n_days)} logged days.")
    if s_days:
        insights.append(f"You average {sleep['avg_hours']} h of sleep. 7 to 9 h is the sweet spot.")

    return {
        "range": {"start": start, "end": end, "days": span},
        "weight": weight,
        "nutrition": nutrition,
        "sleep": sleep,
        "insights": insights,
    }
