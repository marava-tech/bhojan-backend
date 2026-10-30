"""Self-check for the wearable TDEE fallback + energy upload validation. No Mongo needed:
python -m scripts.check_wearable_tdee"""
import asyncio
import os
from datetime import date

os.environ.setdefault("JWT_SECRET", "test")

from pydantic import ValidationError  # noqa: E402

from routers.health_sync import EnergyDay, EnergyUpload  # noqa: E402
from routers.tdee import _with_wearable  # noqa: E402
from services.adaptive_tdee import build_suggestion  # noqa: E402


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, _):
        return self.docs


class _FakeDb:
    def __init__(self, kcal):
        self.energy_burned = self
        self.kcal = kcal

    def find(self, query, projection):
        return _Cursor([{"kcal": k} for k in self.kcal])


async def main():
    thin = {"real_tdee_kcal": None, "confidence": "insufficient"}
    end = date(2026, 10, 29)

    # Enough wearable days + thin logs → wearable becomes maintenance.
    out = await _with_wearable(_FakeDb([1900, 1800, 2100, 1700, 1950, 1850, 1900]), "u", thin, end)
    assert out["confidence"] == "wearable" and out["real_tdee_kcal"] == 1886 and out["wearable_days"] == 7, out

    # Too few wearable days → stays insufficient, average still reported.
    out = await _with_wearable(_FakeDb([1900, 1800]), "u", thin, end)
    assert out["confidence"] == "insufficient" and out["real_tdee_kcal"] is None and out["wearable_tdee_kcal"] == 1850, out

    # A real adaptive estimate is never replaced by the wearable.
    adaptive = {"real_tdee_kcal": 1920, "confidence": "high"}
    out = await _with_wearable(_FakeDb([1500] * 10), "u", adaptive, end)
    assert out["real_tdee_kcal"] == 1920 and out["confidence"] == "high", out

    # Cutter on wearable maintenance → maintenance minus 0.5% bodyweight/week.
    profile = {"goal_kcal": 1700, "tdee_kcal": 2037, "weight_kg": 61}
    s = build_suggestion({"real_tdee_kcal": 1888, "confidence": "wearable", "kg_per_week": 0,
                          "logged_days": 0, "weight_entries": 0}, profile)
    assert s["type"] == "adopt_tdee" and s["suggested_goal_kcal"] == round(1888 - 0.005 * 61 * 7700 / 7), s

    # Upload validation: partial-day / garbage values are rejected, batches are capped.
    EnergyDay(date="2026-10-28", kcal=2104)
    for bad in (500, 9000):
        try:
            EnergyDay(date="2026-10-28", kcal=bad)
            raise AssertionError(f"kcal={bad} accepted")
        except ValidationError:
            pass
    try:
        EnergyUpload(days=[{"date": "2026-10-28", "kcal": 2000}] * 61)
        raise AssertionError("61 days accepted")
    except ValidationError:
        pass
    print("wearable tdee self-check ok")


if __name__ == "__main__":
    asyncio.run(main())
