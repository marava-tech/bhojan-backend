"""Check the TDEE / target pipeline against one real user's logs + a WHOOP export. Read-only on Mongo.

  MONGODB_URI=... python -m scripts.check_tdee_with_data --user-id <id> --whoop-dir <dir> \
      --window-end 2026-07-06

--window-end picks a stretch where the user logged food and weighed in; the WHOOP export gives the
measured energy expenditure the formula is graded against. Exits 1 if any check fails.
"""
import argparse
import asyncio
import csv
import os
import statistics
from datetime import date
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test")

from database import get_db  # noqa: E402
from routers.tdee import _gather_data  # noqa: E402
from services.adaptive_tdee import build_suggestion, compute_real_tdee, detect_plateau  # noqa: E402
from services.tdee import calculate_tdee  # noqa: E402

FORMULA_TOLERANCE = 0.15   # formula TDEE must land within ±15% of WHOOP's measured burn
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def whoop_mean_burn(whoop_dir: Path) -> float:
    # Full days only: a cycle without a recovery score is the first or the still-open one.
    rows = csv.DictReader(open(whoop_dir / "physiological_cycles.csv"))
    return statistics.mean(float(r["Energy burned (cal)"]) for r in rows
                           if r["Energy burned (cal)"] and r["Recovery score %"])


async def main(user_id: str, whoop_dir: Path, window_end: date) -> None:
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})

    # 1. The adaptive TDEE must see the calories that were actually logged.
    intake, weights, weekly = await _gather_data(db, user_id, end=window_end)
    raw = await db.food_logs.aggregate([
        {"$match": {"user_id": user_id, "date": {"$in": list(intake)}}},
        {"$unwind": "$items"},
        {"$group": {"_id": "$date", "kcal": {"$sum": "$items.calories_kcal"}}},
    ]).to_list(None)
    raw_by_date = {d["_id"]: d["kcal"] for d in raw}
    worst = max((abs(intake[d] - raw_by_date.get(d, 0)) for d in intake), default=float("inf"))
    check("intake_read", len(intake) >= 14 and worst < 5,
          f"{len(intake)} days, mean {statistics.mean(intake.values()) if intake else 0:.0f} kcal/day, "
          f"max diff vs summed items {worst:.1f} kcal")

    # 2. Plateau detection must look at the newest weeks of the window, not the oldest.
    latest_weigh_in = date.fromisoformat(max(d for d in weights))
    latest_week = f"{latest_weigh_in.isocalendar().year}-W{latest_weigh_in.isocalendar().week:02d}"
    check("plateau_window_is_recent", bool(weekly) and weekly[-1]["week_start"] == latest_week,
          f"last week used {weekly[-1]['week_start'] if weekly else None}, latest weigh-in week {latest_week}")

    # 3. Adaptive TDEE (logs + weight trend) vs WHOOP's measured burn.
    measured = whoop_mean_burn(whoop_dir)
    insight = compute_real_tdee(intake, weights, window_days=21)
    avg = insight.get("avg_intake_kcal") or 0
    real = insight.get("real_tdee_kcal")
    check("adaptive_tdee_vs_whoop",
          real is not None and insight["confidence"] != "insufficient" and abs(real - measured) / measured <= FORMULA_TOLERANCE,
          f"real_tdee {real} vs WHOOP {measured:.0f} (logged {avg}/day, {insight.get('kg_per_week')} kg/week, "
          f"confidence {insight['confidence']})")

    # 4. The suggestion moves a cutter toward 0.5% bodyweight/week: eat less than logged if losing
    #    slower than that, not less if already losing faster.
    window_kg = statistics.mean(weights.values())
    window_profile = {**profile, "weight_kg": window_kg}
    window_profile["tdee_kcal"] = calculate_tdee(window_kg, profile["height_cm"], profile["age"],
                                                 profile["sex"], len(profile.get("gym_days") or []))["tdee_kcal"]
    suggestion = build_suggestion(insight, window_profile, is_plateau=detect_plateau(weekly))
    goal = suggestion["suggested_goal_kcal"]
    pace = -(insight.get("kg_per_week") or 0) / window_kg
    want_less = pace < 0.005
    ok = avg > 0 and goal is not None and (goal < avg if want_less else goal >= avg - 50)
    check("suggestion_targets_half_pct_per_week", ok,
          f"{suggestion['type']}: suggested {goal} vs logged {avg}; losing {pace:.2%}/week so expected "
          f"{'less' if want_less else 'not less'} than logged (current goal {profile.get('goal_kcal')})")

    # 5. The formula's starting guess vs WHOOP's measured burn.
    formula = calculate_tdee(profile["weight_kg"], profile["height_cm"], profile["age"], profile["sex"],
                             len(profile.get("gym_days") or []))["tdee_kcal"]
    err = (formula - measured) / measured
    check("formula_tdee_vs_whoop", abs(err) <= FORMULA_TOLERANCE,
          f"formula {formula} vs WHOOP {measured:.0f} ({err:+.0%}); stored profile value {profile.get('tdee_kcal')}")

    if real and avg:
        # Logs that undercount by U make the adaptive TDEE come out U below the true burn.
        print(f"INFO  implied logging gap (WHOOP − adaptive TDEE): {measured - real:+.0f} kcal/day")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f" — failed: {', '.join(failed)}" if failed else ""))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--whoop-dir", type=Path, required=True)
    ap.add_argument("--window-end", type=date.fromisoformat, required=True)
    a = ap.parse_args()
    asyncio.run(main(a.user_id, a.whoop_dir, a.window_end))
