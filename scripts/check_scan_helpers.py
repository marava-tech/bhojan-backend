"""Self-check for scan helpers in routers/food.py (no DB, no LLM).

    python -m scripts.check_scan_helpers
"""
import asyncio

from models.food_log import FoodLogCreate
from routers.food import _attach_ai_originals, _scale_to_net_weight


class _FakeScans:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, _query):
        return self.doc


class _FakeDb:
    def __init__(self, doc):
        self.scan_results = _FakeScans(doc)


def main() -> None:
    items = [
        {"name": "rice", "estimated_weight_g": 150, "calories_kcal": 195, "protein_g": 4, "carbs_g": 42, "fat_g": 0.4},
        {"name": "dal", "estimated_weight_g": 50, "calories_kcal": 60, "protein_g": 3, "carbs_g": 7, "fat_g": 2,
         "added_fat_g": 1.5, "pieces": None},
    ]
    scaled = _scale_to_net_weight(items, 300)  # scale says 300 g, model guessed 200 g
    assert [i["estimated_weight_g"] for i in scaled] == [225, 75]
    assert scaled[0]["calories_kcal"] == 292.5 and scaled[1]["added_fat_g"] == 2.2
    assert scaled[1]["pieces"] is None
    assert _scale_to_net_weight(items, 0) is items

    body = FoodLogCreate(meal_slot="lunch", image_url="u", items=[
        {"name": "Rice", "estimated_weight_g": 300, "calories_kcal": 390, "protein_g": 8, "carbs_g": 84, "fat_g": 0.8},
        {"name": "pickle", "estimated_weight_g": 10, "calories_kcal": 20, "protein_g": 0, "carbs_g": 1, "fat_g": 2},
    ])
    ai_kcal = asyncio.run(_attach_ai_originals(body, "user", _FakeDb({"items": items})))
    assert ai_kcal == 255
    # weight doubled vs the scan, so the original is scaled to 300 g: weight edits aren't density corrections
    assert body.items[0].ai_original.calories_kcal == 390
    assert body.items[1].ai_original is None  # added by the user, nothing to compare
    assert asyncio.run(_attach_ai_originals(body, "user", _FakeDb(None))) is None
    print("ok")


if __name__ == "__main__":
    main()
