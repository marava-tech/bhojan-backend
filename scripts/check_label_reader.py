"""Check the nutrition-label reader against label photos with known values.

  OPENROUTER_API_KEY=... python -m scripts.check_label_reader <dir>

<dir> holds images + truth.json: [{"image", "per_100g": {calories_kcal, protein_g, carbs_g, fat_g},
"serving_g": number|null}]. Each value must be within 3% (or 0.5 absolute). Exits 1 on any miss.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from services.gemini import read_nutrition_label


def close(got, want) -> bool:
    if want is None:
        return got is None
    return got is not None and abs(float(got) - want) <= max(0.5, 0.03 * abs(want))


async def main(d: Path) -> None:
    key = os.environ["OPENROUTER_API_KEY"]
    failures = 0
    for case in json.loads((d / "truth.json").read_text()):
        got = await read_nutrition_label((d / case["image"]).read_bytes(), api_key=key)
        per = got.get("per_100g") or {}
        misses = [f"{k} {per.get(k)} != {v}" for k, v in case["per_100g"].items() if not close(per.get(k), v)]
        if not close(got.get("serving_g"), case["serving_g"]):
            misses.append(f"serving_g {got.get('serving_g')} != {case['serving_g']}")
        failures += bool(misses)
        print(f"{'FAIL' if misses else 'PASS'}  {case['image']}" + (f": {'; '.join(misses)}" if misses else ""))
    print(f"\n{failures} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
