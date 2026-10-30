"""Load INDB (Indian Nutrient Databank) recipes into the `foods` collection.

Source: https://github.com/lindsayjaacks/Indian-Nutrient-Databank-INDB- (Vijayakumar et al.,
Curr Dev Nutr 2024). The repo has no licence file, so the data is fetched at import time and never
committed here. Values are per 100 g of the cooked recipe.

    python -m scripts.import_indb            # download, filter, upsert into Mongo
    python -m scripts.import_indb --dry-run  # print what would be loaded / dropped
"""
import argparse
import asyncio
import io
import re
import sys

import httpx
import openpyxl

INDB_URL = "https://raw.githubusercontent.com/lindsayjaacks/Indian-Nutrient-Databank-INDB-/main/INDB.xlsx"


def _aliases(name: str) -> list[str]:
    """'Potato cauliflower (Aloo gobhi)' -> ['potato cauliflower', 'aloo gobhi'];
    'Chapati/Roti' -> ['chapati roti', 'chapati', 'roti']."""
    parts = [name] + re.findall(r"\(([^)]*)\)", name)
    parts[0] = re.sub(r"\([^)]*\)", "", parts[0])
    out: list[str] = []
    for p in parts:
        for v in [p, *p.split("/")] if "/" in p else [p]:
            v = " ".join(re.sub(r"[^a-z ]+", " ", v.lower()).split())
            if v and v not in out:
                out.append(v)
    return out


def _implausible(kcal: float, fat: float) -> bool:
    """INDB counts the whole frying medium as eaten for deep-fried recipes (samosa 577 kcal/100 g,
    59 g fat; real ~300). Drop entries that are mostly fat by energy and very dense."""
    return kcal > 450 and fat * 9 / kcal > 0.75


def parse(xlsx: bytes) -> tuple[list[dict], list[str]]:
    ws = openpyxl.load_workbook(io.BytesIO(xlsx), read_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    ix = {c: i for i, c in enumerate(next(rows))}
    foods, dropped = [], []
    for r in rows:
        code, name = r[ix["food_code"]], r[ix["food_name"]]
        kcal, fat = r[ix["energy_kcal"]], r[ix["fat_g"]]
        if not code or not name or not kcal or kcal <= 0:
            continue
        if _implausible(kcal, fat or 0):
            dropped.append(f"{code} {name} ({kcal:.0f} kcal, {fat:.0f} g fat)")
            continue
        serving_kcal = r[ix["unit_serving_energy_kcal"]]
        foods.append({
            "code": code,
            "name": name,
            "aliases": _aliases(name),
            "per_100g": {
                "calories_kcal": round(kcal, 1),
                "protein_g": round(r[ix["protein_g"]] or 0, 1),
                "carbs_g": round(r[ix["carb_g"]] or 0, 1),
                "fat_g": round(fat or 0, 1),
            },
            "serving_unit": r[ix["servings_unit"]],
            "serving_g": round(serving_kcal / kcal * 100) if serving_kcal else None,
            "source": "INDB",
        })
    return foods, dropped


async def main(dry_run: bool) -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(INDB_URL)
        resp.raise_for_status()
    foods, dropped = parse(resp.content)
    print(f"{len(foods)} foods, {len(dropped)} dropped as implausible:")
    for d in dropped:
        print("  -", d)
    if dry_run:
        return
    from pymongo import ReplaceOne
    from database import connect_db, get_db
    await connect_db()
    db = get_db()
    await db.foods.bulk_write([ReplaceOne({"code": f["code"]}, f, upsert=True) for f in foods])
    await db.foods.create_index("code", unique=True)
    print("upserted", len(foods))


def _selfcheck() -> None:
    assert _aliases("Chapati/Roti") == ["chapati roti", "chapati", "roti"]
    assert _aliases("Potato cauliflower (Aloo gobhi)") == ["potato cauliflower", "aloo gobhi"]
    assert _implausible(577, 59.2) and not _implausible(298, 16.9) and not _implausible(350, 17.7)


if __name__ == "__main__":
    _selfcheck()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
    sys.exit(0)
