"""Calorie-accuracy benchmark: run food-photo pipelines over images with weighed ground truth.

Datasets live in a dir as images/ + truth.jsonl, one row per plate:
  {"id", "image", "kcal", "mass_g", "protein_g", "carbs_g", "fat_g", "foods": [names]}
`--fetch nutrition5k` downloads 100 overhead Nutrition5k plates (every ingredient weighed).
For our own Indian set, drop weighed meal photos in the same format.

Pipelines:
  prod[:<model>]  — the shipped /api/food/analyze prompt (default model or the one named)
  direct:<model>  — one vision call returning items with grams + macros, summed

  python -m scripts.eval_accuracy --data /eval/n5k --fetch nutrition5k
  python -m scripts.eval_accuracy --data /eval/n5k --pipelines prod direct:google/gemini-3.8-flash
  python -m scripts.eval_accuracy --data /eval/n5k --pipelines direct:X --gate direct:X --max-mape 30
  python -m scripts.eval_accuracy --selfcheck

Results append to <data>/results.jsonl, so reruns skip finished (pipeline, id) pairs.
"""
import argparse
import asyncio
import base64
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DIRECT_SYSTEM = (
    "You are an expert dietitian estimating the nutrition of a meal from one photo, with deep "
    "knowledge of Indian home and restaurant cooking as well as global food. You use food "
    "composition data (IFCT 2017, USDA FoodData Central) and you count cooking oil, ghee, butter "
    "and sauces that are absorbed or hidden in the food."
)

DIRECT_PROMPT = """Estimate the nutrition of exactly what is visible in this photo.

- Identify every distinct food item. Composite dishes (e.g. "paneer butter masala") are one item.
- Estimate each item's weight from what you actually see — its area, height and volume relative to
  the plate, bowl, cutlery or hand. Do NOT assume a standard serving size; small or large portions
  must stay small or large.
- Calories and macros are for that weight as served, including absorbed cooking fat.
- Check protein_g*4 + carbs_g*4 + fat_g*9 ≈ calories_kcal for each item.

Return ONLY JSON:
{"items": [{"name": "string", "weight_g": number, "calories_kcal": number, "protein_g": number,
"carbs_g": number, "fat_g": number}]}"""


def _parse_json(text: str) -> dict:
    """First '{' to last '}' — tolerates fences and chatter around the JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON in response: {text[:200]!r}")
    return json.loads(text[start:end + 1])


async def _openrouter(client: httpx.AsyncClient, model: str, image: bytes) -> tuple[dict, float]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DIRECT_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": DIRECT_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}},
            ]},
        ],
        "max_tokens": 4000,
        "usage": {"include": True},
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY'].strip()}"}
    resp = await client.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    body = resp.json()
    if "choices" not in body:
        raise ValueError(f"bad response: {str(body)[:200]}")
    return _parse_json(body["choices"][0]["message"]["content"] or ""), (body.get("usage") or {}).get("cost") or 0.0


def _sum_items(items: list[dict], weight_key: str) -> dict:
    return {
        "kcal": sum(float(i.get("calories_kcal") or 0) for i in items),
        "mass_g": sum(float(i.get(weight_key) or 0) for i in items),
        "protein_g": sum(float(i.get("protein_g") or 0) for i in items),
        "carbs_g": sum(float(i.get("carbs_g") or 0) for i in items),
        "fat_g": sum(float(i.get("fat_g") or 0) for i in items),
    }


async def run_direct(client, model: str, image: bytes) -> dict:
    data, cost = await _openrouter(client, model, image)
    items = data.get("items") or []
    return {**_sum_items(items, "weight_g"), "cost": cost, "items": items}


async def run_prod(client, image: bytes, model: str | None = None) -> dict:
    # The shipped /api/food/analyze prompt (services/gemini.py), minus per-user corrections.
    from services import gemini

    items = (await gemini.analyze_food(image, api_key=os.environ["OPENROUTER_API_KEY"], model=model)).get("items", [])
    return {**_sum_items(items, "estimated_weight_g"), "cost": None, "items": items}


async def predict(client, pipeline: str, image: bytes) -> dict:
    if pipeline == "prod" or pipeline.startswith("prod:"):
        return await run_prod(client, image, pipeline.split(":", 1)[1] if ":" in pipeline else None)
    if pipeline.startswith("direct:"):
        return await run_direct(client, pipeline.split(":", 1)[1], image)
    raise SystemExit(f"unknown pipeline {pipeline}")


def metrics(rows: list[dict]) -> dict:
    """rows: [{"truth": {...}, "pred": {...} | None}] → accuracy summary for one pipeline."""
    ok = [r for r in rows if r.get("pred") and r["truth"]["kcal"] > 0]
    if not ok:
        return {"n": 0, "failed": len(rows)}
    err = [r["pred"]["kcal"] - r["truth"]["kcal"] for r in ok]
    pct = [e / r["truth"]["kcal"] * 100 for e, r in zip(err, ok)]
    costs = [r["pred"]["cost"] for r in ok if r["pred"].get("cost") is not None]
    out = {
        "n": len(ok),
        "failed": len(rows) - len(ok),
        "kcal_mae": round(statistics.mean(abs(e) for e in err), 1),
        "kcal_mape": round(statistics.mean(abs(p) for p in pct), 1),
        "kcal_median_ape": round(statistics.median(abs(p) for p in pct), 1),
        "within_20pct": round(sum(abs(p) <= 20 for p in pct) / len(ok) * 100, 1),
        "bias_pct": round(statistics.mean(pct), 1),  # negative = undercounts
        "mass_mae_g": round(statistics.mean(abs(r["pred"]["mass_g"] - r["truth"]["mass_g"]) for r in ok), 1),
        "latency_p50_s": round(statistics.median(r["latency_s"] for r in ok), 1),
        "usd_per_1k": round(statistics.mean(costs) * 1000, 2) if costs else None,
    }
    for m in ("protein_g", "carbs_g", "fat_g"):
        out[f"{m.split('_')[0]}_mae_g"] = round(statistics.mean(abs(r["pred"][m] - r["truth"][m]) for r in ok), 1)
    return out


async def fetch_nutrition5k(data: Path, split: str = "overhead") -> None:
    """100 Nutrition5k plates via the HF datasets-server (no parquet deps needed)."""
    (data / "images").mkdir(parents=True, exist_ok=True)
    url = "https://datasets-server.huggingface.co/rows"
    params = {"dataset": "madroid/nutrient5k-test-100", "config": "default", "split": split, "offset": 0, "length": 100}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        rows = json.loads(resp.text, strict=False)["rows"]
        with open(data / "truth.jsonl", "w") as f:
            for r in rows:
                row = r["row"]
                total = row["actual"]["total"]
                img = await client.get(row["image"]["src"])
                img.raise_for_status()
                name = f"{r['row_idx']:03d}.jpg"
                (data / "images" / name).write_bytes(img.content)
                f.write(json.dumps({
                    "id": row.get("name") or name, "image": name,
                    "kcal": total["calories"], "mass_g": total["size"],
                    "protein_g": total["protein"], "carbs_g": total["carbs"], "fat_g": total["fat"],
                    "foods": [x["name"] for x in row["actual"].get("foods") or []],
                }) + "\n")
    print(f"fetched {len(rows)} plates → {data}")


async def run(data: Path, pipelines: list[str], concurrency: int, limit: int | None) -> None:
    truth = [json.loads(line) for line in open(data / "truth.jsonl")][:limit]
    results_path = data / "results.jsonl"
    done = set()
    if results_path.exists():
        for line in open(results_path):
            r = json.loads(line)
            if r.get("pred"):
                done.add((r["pipeline"], r["id"]))

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one(client, pipeline, t):
        async with sem:
            start = time.monotonic()
            try:
                pred, error = await predict(client, pipeline, (data / "images" / t["image"]).read_bytes()), None
            except Exception as e:  # one bad response shouldn't sink the run; it counts as a failure
                pred, error = None, f"{type(e).__name__}: {e}"[:300]
            row = {"pipeline": pipeline, "id": t["id"], "truth": t, "pred": pred, "error": error,
                   "latency_s": round(time.monotonic() - start, 2)}
            async with lock:
                with open(results_path, "a") as f:
                    f.write(json.dumps(row) + "\n")
            status = f"{pred['kcal']:.0f} vs {t['kcal']:.0f}" if pred else error
            print(f"[{pipeline}] {t['id']}: {status}", flush=True)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[one(client, p, t) for p in pipelines for t in truth if (p, t["id"]) not in done])


def summarize(data: Path, ids: set | None) -> dict:
    """Latest row per (pipeline, id) → metrics per pipeline, printed as a table sorted by MAPE."""
    latest = {}
    for line in open(data / "results.jsonl"):
        r = json.loads(line)
        if ids is None or r["id"] in ids:
            latest[(r["pipeline"], r["id"])] = r
    by_pipeline = {}
    for (p, _), r in latest.items():
        by_pipeline.setdefault(p, []).append(r)
    summary = {p: metrics(rows) for p, rows in by_pipeline.items()}
    cols = ["n", "failed", "kcal_mape", "kcal_median_ape", "within_20pct", "bias_pct", "kcal_mae",
            "mass_mae_g", "protein_mae_g", "carbs_mae_g", "fat_mae_g", "latency_p50_s", "usd_per_1k"]
    print("\n" + "pipeline".ljust(42) + "".join(c[:14].rjust(15) for c in cols))
    for p, m in sorted(summary.items(), key=lambda kv: kv[1].get("kcal_mape", 1e9)):
        print(p.ljust(42) + "".join(str(m.get(c, "-")).rjust(15) for c in cols))
    (data / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def selfcheck() -> None:
    t = {"kcal": 500, "mass_g": 300, "protein_g": 30, "carbs_g": 50, "fat_g": 20}
    rows = [
        {"truth": t, "pred": {**t, "kcal": 400, "cost": 0.001}, "latency_s": 2},   # -20%
        {"truth": t, "pred": {**t, "kcal": 650, "cost": 0.003}, "latency_s": 4},   # +30%
        {"truth": t, "pred": None, "latency_s": 9},
    ]
    m = metrics(rows)
    assert m["n"] == 2 and m["failed"] == 1, m
    assert m["kcal_mae"] == 125 and m["kcal_mape"] == 25 and m["bias_pct"] == 5, m
    assert m["within_20pct"] == 50 and m["usd_per_1k"] == 2.0, m
    assert _parse_json('```json\n{"items": []}\n```') == {"items": []}
    assert _sum_items([{"calories_kcal": 100, "weight_g": 50}, {"calories_kcal": "20"}], "weight_g")["kcal"] == 120
    print("selfcheck ok")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path)
    ap.add_argument("--fetch", choices=["nutrition5k"])
    ap.add_argument("--pipelines", nargs="*", default=[])
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, help="only the first N plates")
    ap.add_argument("--gate", help="pipeline that must meet --max-mape, else exit 1")
    ap.add_argument("--max-mape", type=float, default=30.0)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()
    if args.fetch:
        asyncio.run(fetch_nutrition5k(args.data))
    if args.pipelines:
        asyncio.run(run(args.data, args.pipelines, args.concurrency, args.limit))
    if (args.data / "results.jsonl").exists():
        ids = {json.loads(line)["id"] for line in list(open(args.data / "truth.jsonl"))[:args.limit]}
        summary = summarize(args.data, ids)
        if args.gate:
            mape = summary.get(args.gate, {}).get("kcal_mape")
            if mape is None or mape > args.max_mape:
                print(f"GATE FAILED: {args.gate} MAPE {mape} > {args.max_mape}")
                sys.exit(1)
            print(f"gate ok: {args.gate} MAPE {mape} <= {args.max_mape}")


if __name__ == "__main__":
    main()
