"""Food logging — AI analysis pipeline + CRUD for logs."""
import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, File, Form

from bson import ObjectId
from typing import Optional

from auth import get_current_user
from database import get_db
from models.food_log import (AiOriginalMacros, DescribeFoodRequest, FoodLogCreate, FoodItem, ItemEstimateRequest,
                             MacroSource, MealSlot)
from services import ai_quota
from services import gemini as gemini_svc
from services import minio_client
from services.openfoodfacts import lookup_barcode, lookup_macros
from utils import validate_image_upload, get_openrouter_key

# Correction-blend config
_CORRECTION_EPSILON = 0.05   # ignore edits < 5% relative change (noise threshold)
_CORRECTION_MAX_BLEND = 0.40  # cap blend at ±40% to prevent runaway corrections

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/food", tags=["food"])


async def _resolve_macros(
    name: str,
    weight_g: float,
    api_key: str | None,
    cooking_method: str | None = None,
    source_type: str | None = None,
    user_id: str | None = None,
) -> dict:
    result = await lookup_macros(name, weight_g)
    if result:
        return result
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")
    ai_result = await gemini_svc.estimate_macros(
        name, weight_g, api_key=api_key,
        cooking_method=cooking_method, source_type=source_type,
    )
    ai_result["source"] = "ai_estimated"
    if user_id:
        await _personalize(ai_result, name, weight_g, user_id)
    return ai_result


async def _personalize(macros: dict, name: str, weight_g: float, user_id: str) -> None:
    """Blend in the user's past corrections for this food name, in place."""
    correction = await get_db().food_corrections.find_one({"user_id": user_id, "name_norm": name.strip().lower()})
    if not correction or not macros.get("calories_kcal"):
        return
    # Weight blend toward correction proportional to confidence (capped at 0.6 after 5+ edits)
    blend_weight = min(0.12 * correction.get("count", 1), 0.60)
    corr = correction["corrected_per_g"]

    def _apply(ai_val: float, corr_per_g: float) -> float:
        corrected = corr_per_g * weight_g
        delta_ratio = (corrected - ai_val) / ai_val if ai_val else 0.0
        # Cap blend delta at ±_CORRECTION_MAX_BLEND to prevent runaway
        clamped_delta = max(-_CORRECTION_MAX_BLEND, min(_CORRECTION_MAX_BLEND, delta_ratio))
        return round(ai_val * (1 + clamped_delta * blend_weight), 1)

    for key, corr_key in (("calories_kcal", "cal_per_g"), ("protein_g", "protein_per_g"),
                          ("carbs_g", "carbs_per_g"), ("fat_g", "fat_per_g")):
        macros[key] = _apply(float(macros.get(key) or 0), corr[corr_key])
    macros["personalized"] = True


async def _price_items(items: list[dict], api_key: str, user_id: str, source_type: str | None = None) -> list[dict]:
    """Items from a single-call analysis already carry macros; apply personal corrections.
    An item the model returned without calories falls back to the per-item lookup."""
    async def one(item: dict) -> dict:
        weight = float(item.get("estimated_weight_g") or 0)
        if item.get("calories_kcal") is None or weight <= 0:
            macros = await _resolve_macros(item["name"], weight or 100.0, api_key,
                                           cooking_method=item.get("cooking_method"),
                                           source_type=source_type, user_id=user_id)
            return {**item, **macros}
        macros = {k: round(float(item.get(k) or 0), 1) for k in ("calories_kcal", "protein_g", "carbs_g", "fat_g")}
        macros["source"] = "ai_estimated"
        await _personalize(macros, item["name"], weight, user_id)
        return {**item, **macros}

    return await asyncio.gather(*[one(i) for i in items])


def _scale_to_net_weight(items: list[dict], net_weight_g: float) -> list[dict]:
    """Scale every item so the plate totals the weighed net grams. Weight comes from the scale;
    the model only splits it between items and supplies per-gram density."""
    total = sum(float(i.get("estimated_weight_g") or 0) for i in items)
    if total <= 0 or net_weight_g <= 0:
        return items
    f = net_weight_g / total
    keys = ("estimated_weight_g", "calories_kcal", "protein_g", "carbs_g", "fat_g", "added_fat_g")
    return [{**i, **{k: round(float(i[k]) * f, 1) for k in keys if i.get(k) is not None}} for i in items]


async def _attach_ai_originals(body: FoodLogCreate, user_id: str, db) -> float | None:
    """The app doesn't send ai_original, so look up what /analyze returned for this photo and attach
    it per item (scaled to the saved weight, so only density edits count as corrections).
    Returns the scan's total kcal, or None when this log didn't come from a scan."""
    if not body.image_url:
        return None
    scan = await db.scan_results.find_one({"user_id": user_id, "image_url": body.image_url})
    if not scan:
        return None
    by_name = {(i.get("name") or "").strip().lower(): i for i in scan["items"]}
    for item in body.items:
        orig = by_name.get(item.name.strip().lower())
        w0 = float((orig or {}).get("estimated_weight_g") or 0)
        if item.ai_original is None and orig and w0 > 0:
            f = item.estimated_weight_g / w0
            item.ai_original = AiOriginalMacros(**{k: float(orig.get(k) or 0) * f
                                                   for k in ("calories_kcal", "protein_g", "carbs_g", "fat_g")})
    return round(sum(float(i.get("calories_kcal") or 0) for i in scan["items"]), 1)


async def _capture_corrections(items: list, user_id: str, db) -> None:
    """For each item that has an ai_original snapshot, compare per-gram macros with what
    the user actually saved.  If the relative change exceeds the noise threshold, upsert a
    running-average correction record in food_corrections so future estimates can be biased
    toward the user's preferred values.
    """
    for item in items:
        orig = item.get("ai_original") if isinstance(item, dict) else None
        if orig is None:
            continue
        weight = float(item.get("estimated_weight_g") or 0)
        if weight <= 0:
            continue

        # Current (user-accepted) per-gram values
        cur_cal = float(item.get("calories_kcal") or 0) / weight
        cur_prot = float(item.get("protein_g") or 0) / weight
        cur_carbs = float(item.get("carbs_g") or 0) / weight
        cur_fat = float(item.get("fat_g") or 0) / weight

        # Original AI per-gram values
        if isinstance(orig, dict):
            ai_cal = float(orig.get("calories_kcal") or 0) / weight
            ai_prot = float(orig.get("protein_g") or 0) / weight
            ai_carbs = float(orig.get("carbs_g") or 0) / weight
            ai_fat = float(orig.get("fat_g") or 0) / weight
        else:
            ai_cal = float(orig.calories_kcal) / weight
            ai_prot = float(orig.protein_g) / weight
            ai_carbs = float(orig.carbs_g) / weight
            ai_fat = float(orig.fat_g) / weight

        # Only record if at least one macro changed meaningfully
        if ai_cal <= 0:
            continue
        rel_change = abs(cur_cal - ai_cal) / ai_cal
        if rel_change < _CORRECTION_EPSILON:
            continue

        name_norm = item.get("name", "").strip().lower()
        if not name_norm:
            continue

        # Upsert a running average (weighted by count) of the corrected per-gram values
        existing = await db.food_corrections.find_one({"user_id": user_id, "name_norm": name_norm})
        if existing:
            n = existing.get("count", 1)
            def _blend(old: float, new: float, n: int) -> float:
                return (old * n + new) / (n + 1)

            await db.food_corrections.update_one(
                {"user_id": user_id, "name_norm": name_norm},
                {"$set": {
                    "corrected_per_g.cal_per_g": _blend(existing["corrected_per_g"]["cal_per_g"], cur_cal, n),
                    "corrected_per_g.protein_per_g": _blend(existing["corrected_per_g"]["protein_per_g"], cur_prot, n),
                    "corrected_per_g.carbs_per_g": _blend(existing["corrected_per_g"]["carbs_per_g"], cur_carbs, n),
                    "corrected_per_g.fat_per_g": _blend(existing["corrected_per_g"]["fat_per_g"], cur_fat, n),
                    "count": n + 1,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
        else:
            await db.food_corrections.insert_one({
                "user_id": user_id,
                "name_norm": name_norm,
                "original_per_g": {
                    "cal_per_g": ai_cal,
                    "protein_per_g": ai_prot,
                    "carbs_per_g": ai_carbs,
                    "fat_per_g": ai_fat,
                },
                "corrected_per_g": {
                    "cal_per_g": cur_cal,
                    "protein_per_g": cur_prot,
                    "carbs_per_g": cur_carbs,
                    "fat_per_g": cur_fat,
                },
                "count": 1,
                "updated_at": datetime.now(timezone.utc),
            })


async def _update_if_log(food_date: str, timestamp: datetime, user_id: str, db):
    """Derive IF adherence from the eating window in user_profile."""
    profile = await db.user_profile.find_one({"user_id": user_id})
    if not profile:
        return

    window_start = profile.get("eating_window_start", "13:00")
    window_end = profile.get("eating_window_end", "21:00")
    tz_name = profile.get("user_timezone", "UTC")

    start_h, start_m = map(int, window_start.split(":"))
    end_h, end_m = map(int, window_end.split(":"))

    # Convert UTC timestamp to the user's local timezone before comparison
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown user_timezone '%s', falling back to UTC", tz_name)
        user_tz = ZoneInfo("UTC")

    local_time = timestamp.astimezone(user_tz)
    food_time = local_time.hour * 60 + local_time.minute
    window_start_min = start_h * 60 + start_m
    window_end_min = end_h * 60 + end_m

    if window_start_min <= window_end_min:
        in_window = window_start_min <= food_time <= window_end_min
    else:
        # Cross-midnight window (e.g. 22:00–06:00)
        in_window = food_time >= window_start_min or food_time <= window_end_min

    # Atomic upsert: adherence stays True only if every entry so far has been
    # in-window (defaults to True on insert), avoiding a read-then-write race
    # that could otherwise create duplicate if_logs docs for the same day.
    await db.if_logs.update_one(
        {"date": food_date, "user_id": user_id},
        [{"$set": {"adhered": {"$and": [{"$ifNull": ["$adhered", True]}, in_window]}}}],
        upsert=True,
    )


@router.post("/analyze")
async def analyze_food(
    photo: UploadFile = File(...),
    model: Optional[str] = Form(None),
    image_url: Optional[str] = Form(None),
    net_weight_g: Optional[float] = Form(None),
    lang: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user),
    _quota: None = Depends(ai_quota.photo_scan_gate),
):
    db = get_db()
    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)

    # Fetch user's API key (falls back to server-wide key for internal testing)
    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    # Only upload to MinIO on the first pass — a retry with a different model reuses
    # the image_url from the original analysis instead of storing a duplicate copy.
    if not image_url:
        filename = f"{uuid.uuid4()}.jpg"
        image_url = await minio_client.upload_image(image_bytes, minio_client.BUCKET_FOOD, filename)

    retry_model = gemini_svc.RETRY_VISION_MODELS.get(model) if model else None

    # AI food analysis
    analysis = await gemini_svc.analyze_food(image_bytes, api_key=api_key, model=retry_model, lang=lang)
    items = analysis.get("items", [])
    scale_weight_g = analysis.get("scale_weight_g")

    # Saved meal similarity check — only fetch name + item names (scoped to user)
    saved_meals = await db.saved_meals.find(
        {"user_id": user_id}, {"_id": 1, "name": 1, "items.name": 1}
    ).to_list(None)
    meal_suggestion = None
    if saved_meals and items:
        detected_names = {item["name"].lower() for item in items}
        best_overlap = 0.0
        best_meal = None
        for meal in saved_meals:
            meal_names = {i["name"].lower() for i in meal.get("items", [])}
            if not meal_names:
                continue
            overlap = len(detected_names & meal_names) / max(len(meal_names), 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_meal = meal
        if best_overlap >= 0.6 and best_meal:
            meal_suggestion = {
                "meal_id": str(best_meal["_id"]),
                "meal_name": best_meal["name"],
                "overlap": round(best_overlap, 2),
            }

    enriched_items = await _price_items(items, api_key, user_id)
    if net_weight_g:
        enriched_items = _scale_to_net_weight(enriched_items, net_weight_g)
    # Kept so /logs can tell what the user edited (accuracy metric + correction learning)
    await db.scan_results.update_one(
        {"user_id": user_id, "image_url": image_url},
        {"$set": {"items": enriched_items, "model": retry_model or gemini_svc._VISION_MODEL,
                  "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

    return {
        "items": enriched_items,
        "scale_weight_g": scale_weight_g,
        "image_url": image_url,
        "meal_suggestion": meal_suggestion,
    }


# Demo scan limits — in memory, so a restart resets them (fine for a cost guard)
_DEMO_PER_IP = 3
_DEMO_DAILY_CAP = 500
_demo_hits: dict[str, int] = {}
_demo_day = ""


@router.post("/analyze-demo")
async def analyze_demo(request: Request, photo: UploadFile = File(...), lang: Optional[str] = Form(None)):
    """Try one scan before signing up: no account, nothing stored, a few per IP per day.
    Same response shape as /analyze."""
    global _demo_day
    today = date.today().isoformat()
    if today != _demo_day:
        _demo_hits.clear()
        _demo_day = today
    # Behind the Cloudflare tunnel the real client IP is in CF-Connecting-IP
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
    if _demo_hits.get(ip, 0) >= _DEMO_PER_IP or sum(_demo_hits.values()) >= _DEMO_DAILY_CAP:
        raise HTTPException(429, "Demo limit reached — sign up to keep scanning")
    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)
    api_key = get_openrouter_key(None)
    if not api_key:
        raise HTTPException(503, "Demo scan is unavailable")
    _demo_hits[ip] = _demo_hits.get(ip, 0) + 1  # count before the call so parallel requests can't slip past
    analysis = await gemini_svc.analyze_food(image_bytes, api_key=api_key, lang=lang)
    items = [{**i, "source": "ai_estimated"} for i in analysis.get("items", []) if i.get("calories_kcal") is not None]
    return {"items": items, "scale_weight_g": analysis.get("scale_weight_g"), "image_url": None, "meal_suggestion": None}


@router.post("/describe")
async def describe_food(
    body: DescribeFoodRequest,
    user_id: str = Depends(get_current_user),
    _quota: None = Depends(ai_quota.scan_gate),
):
    """Parse a free-text meal description (e.g. '100g cooked chicken with 2 tsp oil') into items
    with resolved macros — the text-only counterpart to /analyze, with no photo involved.
    """
    db = get_db()
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Description text is required")

    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    source_type = body.source_type.value if body.source_type else None
    parsed = await gemini_svc.describe_food(text, api_key=api_key, source_type=source_type, lang=body.lang)
    items = parsed.get("items", [])
    if not items:
        raise HTTPException(422, "Couldn't identify any food items in that description")

    enriched_items = await _price_items(items, api_key, user_id, source_type)

    return {"items": enriched_items}


_BARCODE_RE = re.compile(r"^\d{8,14}$")
_BARCODE_REFRESH_DAYS = 30


def _check_barcode(code: str) -> None:
    if not _BARCODE_RE.match(code):
        raise HTTPException(400, "Barcode must be 8–14 digits")


def _barcode_out(code: str, doc: dict) -> dict:
    return {
        "barcode": code,
        "name": doc.get("name"),
        "brand": doc.get("brand"),
        "per_100g": doc["per_100g"],
        "serving_g": doc.get("serving_g"),
        "image_url": doc.get("image_url"),
        "source": doc["source"],  # "openfoodfacts" | "label"
    }


@router.get("/barcode/{code}")
async def get_barcode_product(code: str, user_id: str = Depends(get_current_user)):
    """Packaged food by barcode: the user's own label scan, else OpenFoodFacts (cached, shared)."""
    _check_barcode(code)
    db = get_db()
    # The user's own label read wins — it's what's printed on the pack in their hand.
    own = await db.barcode_products.find_one({"barcode": code, "user_id": user_id})
    if own:
        return _barcode_out(code, own)

    shared = await db.barcode_products.find_one({"barcode": code, "user_id": None})
    fresh_after = (date.today() - timedelta(days=_BARCODE_REFRESH_DAYS)).isoformat()
    if shared and shared.get("fetched_on", "") >= fresh_after:
        return _barcode_out(code, shared)

    found = await lookup_barcode(code)
    if not found:
        if shared:  # OpenFoodFacts down or product removed — stale beats nothing
            return _barcode_out(code, shared)
        raise HTTPException(404, "Product not found. Scan its nutrition label instead.")
    doc = {**found, "source": "openfoodfacts", "fetched_on": date.today().isoformat()}
    await db.barcode_products.update_one({"barcode": code, "user_id": None}, {"$set": doc}, upsert=True)
    return _barcode_out(code, doc)


@router.post("/barcode/{code}/label")
async def read_barcode_label(
    code: str,
    photo: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    _quota: None = Depends(ai_quota.scan_gate),
):
    """Read the nutrition label for a barcode OpenFoodFacts doesn't know (common for Indian packs),
    and remember it for this user so the next scan of that barcode is instant."""
    _check_barcode(code)
    db = get_db()
    image_bytes = await photo.read()
    validate_image_upload(image_bytes, photo.filename or "", photo.content_type)
    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    data = await gemini_svc.read_nutrition_label(image_bytes, api_key=api_key)
    per = data.get("per_100g") or {}
    try:
        per = {k: round(float(per[k]), 1) for k in ("calories_kcal", "protein_g", "carbs_g", "fat_g")}
    except (KeyError, TypeError, ValueError):
        per = None
    # Nothing edible is above ~900 kcal/100g (pure fat); negative or empty means a misread.
    if data.get("error") or not per or not (0 < per["calories_kcal"] <= 950) or min(per.values()) < 0:
        raise HTTPException(422, "Couldn't read that label. Try a closer, sharper photo of the nutrition table.")

    shared = await db.barcode_products.find_one({"barcode": code, "user_id": None}) or {}
    serving = data.get("serving_g")
    doc = {
        "name": data.get("name") or shared.get("name") or f"Product {code}",
        "brand": shared.get("brand"),
        "per_100g": per,
        "serving_g": float(serving) if isinstance(serving, (int, float)) and serving > 0 else None,
        "image_url": shared.get("image_url"),
        "source": "label",
        "fetched_on": date.today().isoformat(),
    }
    await db.barcode_products.update_one({"barcode": code, "user_id": user_id}, {"$set": doc}, upsert=True)
    return _barcode_out(code, doc)


@router.post("/estimate-item")
async def estimate_item(body: ItemEstimateRequest, user_id: str = Depends(get_current_user)):
    """Re-estimate one item's macros once cooking method / home-vs-restaurant is known.

    The initial /analyze pass runs on the photo alone, before the user has set that
    context on the review screen, so this lets the client refresh the numbers in place.
    """
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)
    if not api_key:
        raise HTTPException(402, "Set your OpenRouter API key in Settings to use AI features")

    macros = await _resolve_macros(
        body.name, body.estimated_weight_g, api_key,
        cooking_method=body.cooking_method.value if body.cooking_method else None,
        source_type=body.source_type.value if body.source_type else None,
        user_id=user_id,
    )
    return macros


@router.post("/logs", status_code=201)
async def create_food_log(body: FoodLogCreate, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    db = get_db()
    now = datetime.now(timezone.utc)

    # Derive the log date in the user's local timezone so midnight-crossover entries
    # land on the correct day (e.g. 1 AM IST is still the same calendar day for the user).
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")

    if body.date:
        try:
            food_date = date.fromisoformat(body.date).isoformat()
        except ValueError:
            raise HTTPException(400, "Invalid date, expected YYYY-MM-DD")
    else:
        # No explicit day supplied (e.g. logging from the home screen) — use today.
        food_date = now.astimezone(user_tz).date().isoformat()

    # Fetch user's API key — only needed if OpenFoodFacts misses an item
    api_key = get_openrouter_key(profile)
    ai_kcal = await _attach_ai_originals(body, user_id, db)

    # Resolve macros — skip lookup for items that already carry all four macro values
    # (e.g. supplements logged from the quiz with macro_source='database').
    resolved_items = []
    total_cal = total_protein = total_carbs = total_fat = 0.0

    items_needing_lookup = [
        item for item in body.items
        if not (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None)
    ]
    macro_lookup_results = await asyncio.gather(*[
        _resolve_macros(
            item.name, item.estimated_weight_g, api_key,
            cooking_method=item.cooking_method.value if item.cooking_method else None,
            source_type=body.source_type.value if body.source_type else None,
            user_id=user_id,
        )
        for item in items_needing_lookup
    ])
    lookup_iter = iter(macro_lookup_results)

    for item in body.items:
        resolved = item.model_dump()
        if (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None):
            macros = {
                "calories_kcal": item.calories_kcal,
                "protein_g": item.protein_g,
                "carbs_g": item.carbs_g,
                "fat_g": item.fat_g,
            }
        else:
            macros = next(lookup_iter)
            resolved.update(macros)
        # Strip client-only correction snapshot before DB write
        resolved.pop("ai_original", None)
        resolved_items.append(resolved)
        total_cal += macros.get("calories_kcal", 0)
        total_protein += macros.get("protein_g", 0)
        total_carbs += macros.get("carbs_g", 0)
        total_fat += macros.get("fat_g", 0)

    doc = {
        "user_id": user_id,
        "date": food_date,
        "meal_slot": body.meal_slot,
        "source_type": body.source_type.value if body.source_type else None,
        "items": resolved_items,
        "image_url": body.image_url,
        "note": body.note,
        "totals": {
            "calories_kcal": round(total_cal, 1),
            "protein_g": round(total_protein, 1),
            "carbs_g": round(total_carbs, 1),
            "fat_g": round(total_fat, 1),
        },
        "ai_kcal": ai_kcal,  # scan's own total; totals vs this = how much the user corrected
        "created_at": now,
    }

    # Supplements are submitted once per day from the daily quiz.
    # Always clear previous supplement food logs on re-submit, then only insert if
    # there are actual items (handles the case where no supplements were taken).
    if body.meal_slot == MealSlot.supplement:
        await db.food_logs.delete_many({"date": food_date, "meal_slot": MealSlot.supplement.value, "user_id": user_id})
        if not body.items:
            return {"date": food_date, "meal_slot": "supplement", "items": [], "totals": {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}}

    result = await db.food_logs.insert_one(doc)

    background_tasks.add_task(_update_if_log, food_date, now, user_id, db)
    # Capture any user corrections vs AI estimate for the learning loop
    items_with_originals = [item.model_dump() for item in body.items]
    background_tasks.add_task(_capture_corrections, items_with_originals, user_id, db)

    doc["_id"] = str(result.inserted_id)
    return doc


@router.delete("/logs/{log_id}", status_code=204)
async def delete_food_log(log_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    try:
        oid = ObjectId(log_id)
    except Exception:
        raise HTTPException(400, "Invalid log ID")
    result = await db.food_logs.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Food log not found")


@router.put("/logs/{log_id}")
async def update_food_log(
    log_id: str,
    body: FoodLogCreate,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user),
):
    db = get_db()
    try:
        oid = ObjectId(log_id)
    except Exception:
        raise HTTPException(400, "Invalid log ID")

    # Check if the log exists and belongs to the user
    existing_log = await db.food_logs.find_one({"_id": oid, "user_id": user_id})
    if not existing_log:
        raise HTTPException(404, "Food log not found")

    profile = await db.user_profile.find_one({"user_id": user_id})
    api_key = get_openrouter_key(profile)

    resolved_items = []
    total_cal = total_protein = total_carbs = total_fat = 0.0

    items_needing_lookup = [
        item for item in body.items
        if not (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None)
    ]
    macro_lookup_results = await asyncio.gather(*[
        _resolve_macros(
            item.name, item.estimated_weight_g, api_key,
            cooking_method=item.cooking_method.value if item.cooking_method else None,
            source_type=body.source_type.value if body.source_type else None,
            user_id=user_id,
        )
        for item in items_needing_lookup
    ])
    lookup_iter = iter(macro_lookup_results)

    for item in body.items:
        resolved = item.model_dump()
        if (item.calories_kcal is not None and item.protein_g is not None
                and item.carbs_g is not None and item.fat_g is not None):
            macros = {
                "calories_kcal": item.calories_kcal,
                "protein_g": item.protein_g,
                "carbs_g": item.carbs_g,
                "fat_g": item.fat_g,
            }
        else:
            macros = next(lookup_iter)
            resolved.update(macros)
        # Strip client-only correction snapshot before DB write
        resolved.pop("ai_original", None)
        resolved_items.append(resolved)
        total_cal += macros.get("calories_kcal", 0)
        total_protein += macros.get("protein_g", 0)
        total_carbs += macros.get("carbs_g", 0)
        total_fat += macros.get("fat_g", 0)

    update_doc = {
        "meal_slot": body.meal_slot,
        "items": resolved_items,
        "note": body.note,
        "totals": {
            "calories_kcal": round(total_cal, 1),
            "protein_g": round(total_protein, 1),
            "carbs_g": round(total_carbs, 1),
            "fat_g": round(total_fat, 1),
        },
        "updated_at": datetime.now(timezone.utc),
    }
    if body.image_url is not None:
        update_doc["image_url"] = body.image_url

    await db.food_logs.update_one({"_id": oid, "user_id": user_id}, {"$set": update_doc})

    log_date = existing_log.get("date")
    if log_date:
        background_tasks.add_task(
            _update_if_log,
            log_date,
            existing_log.get("created_at") or datetime.now(timezone.utc),
            user_id,
            db,
        )
    # Capture any user corrections vs AI estimate for the learning loop
    items_with_originals = [item.model_dump() for item in body.items]
    background_tasks.add_task(_capture_corrections, items_with_originals, user_id, db)

    updated = await db.food_logs.find_one({"_id": oid})
    if updated:
        updated["_id"] = str(updated["_id"])
        if "created_at" in updated and isinstance(updated["created_at"], datetime):
            updated["created_at"] = updated["created_at"].isoformat()
        if "updated_at" in updated and isinstance(updated["updated_at"], datetime):
            updated["updated_at"] = updated["updated_at"].isoformat()
    return updated


@router.get("/logs")
async def get_food_logs(date: str, user_id: str = Depends(get_current_user)):
    """date format: YYYY-MM-DD (query param ?date=)"""
    db = get_db()
    docs = await db.food_logs.find({"date": date, "user_id": user_id}).to_list(None)

    grouped: dict[str, list] = {"breakfast": [], "lunch": [], "dinner": [], "extras": [], "supplement": [], "meal1": [], "meal2": [], "snack": []}
    slot_totals: dict[str, dict] = {}

    for doc in docs:
        slot = doc.get("meal_slot", "snack")
        doc["_id"] = str(doc["_id"])
        grouped.setdefault(slot, []).append(doc)

    # Calculate per-slot totals
    day_total = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for slot, entries in grouped.items():
        t = {"calories_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        for entry in entries:
            totals = entry.get("totals", {})
            for k in t:
                t[k] += totals.get(k, 0)
                day_total[k] += totals.get(k, 0)
        slot_totals[slot] = {k: round(v, 1) for k, v in t.items()}

    return {
        "date": date,
        "entries": grouped,
        "slot_totals": slot_totals,
        "day_total": {k: round(v, 1) for k, v in day_total.items()},
    }


@router.get("/daily-totals")
async def get_daily_totals(days: int = 30, user_id: str = Depends(get_current_user)):
    """Returns daily aggregated calories, protein, carbs, and fat for the past N days."""
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")
    today = datetime.now(user_tz).date()
    match_stage: dict = {"user_id": user_id}
    if days > 0:
        start = (today - timedelta(days=days - 1)).isoformat()
        match_stage["date"] = {"$gte": start, "$lte": today.isoformat()}

    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": "$date",
            "calories_kcal": {"$sum": "$totals.calories_kcal"},
            "protein_g": {"$sum": "$totals.protein_g"},
            "carbs_g": {"$sum": "$totals.carbs_g"},
            "fat_g": {"$sum": "$totals.fat_g"},
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "calories_kcal": {"$round": ["$calories_kcal", 1]},
            "protein_g": {"$round": ["$protein_g", 1]},
            "carbs_g": {"$round": ["$carbs_g", 1]},
            "fat_g": {"$round": ["$fat_g", 1]},
        }},
        {"$sort": {"date": 1}},
    ]

    result = await db.food_logs.aggregate(pipeline).to_list(None)
    return {"totals": result}


@router.get("/daily-totals-by-slot")
async def get_daily_totals_by_slot(
    start: str, end: str, user_id: str = Depends(get_current_user)
):
    """Same as /daily-totals but broken out per meal_slot, over an explicit
    [start, end] date range — used to sync each meal to Health Connect under
    its correct meal type (breakfast/lunch/dinner/other). The caller resolves
    the range itself: account-creation-to-now for a first sync, or
    last-sync-to-now for a catch-up sync."""
    db = get_db()
    match_stage: dict = {"user_id": user_id, "date": {"$gte": start, "$lte": end}}

    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": {"date": "$date", "meal_slot": "$meal_slot"},
            "calories_kcal": {"$sum": "$totals.calories_kcal"},
            "protein_g": {"$sum": "$totals.protein_g"},
            "carbs_g": {"$sum": "$totals.carbs_g"},
            "fat_g": {"$sum": "$totals.fat_g"},
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id.date",
            "meal_slot": "$_id.meal_slot",
            "calories_kcal": {"$round": ["$calories_kcal", 1]},
            "protein_g": {"$round": ["$protein_g", 1]},
            "carbs_g": {"$round": ["$carbs_g", 1]},
            "fat_g": {"$round": ["$fat_g", 1]},
        }},
        {"$sort": {"date": 1}},
    ]

    result = await db.food_logs.aggregate(pipeline).to_list(None)
    return {"totals": result}


@router.get("/logging-score")
async def get_logging_score(days: int = 7, user_id: str = Depends(get_current_user)):
    """Returns a logging consistency score (0–100) for the last N days.

    Score = (logged_days / total_days) * 100, plus a bonus for multi-meal coverage.
    Also includes a 7-day average kcal for the weekly headline.
    """
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    tz_name = (profile or {}).get("user_timezone", "UTC")
    try:
        user_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        user_tz = ZoneInfo("UTC")

    today = datetime.now(user_tz).date()
    start = (today - timedelta(days=days - 1)).isoformat()

    pipeline = [
        {"$match": {"user_id": user_id, "date": {"$gte": start, "$lte": today.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "total_kcal": {"$sum": "$totals.calories_kcal"},
            "meal_slots": {"$addToSet": "$meal_slot"},
        }},
    ]
    day_docs = await db.food_logs.aggregate(pipeline).to_list(None)

    logged_days = len(day_docs)
    total_days = days

    avg_kcal = (
        sum(d.get("total_kcal", 0) for d in day_docs) / logged_days
        if logged_days > 0
        else None
    )

    # Bonus: average meal slot coverage per logged day
    slot_coverage = (
        sum(len(d.get("meal_slots", [])) for d in day_docs) / logged_days
        if logged_days > 0
        else 0
    )

    # Base score: coverage ratio
    base = (logged_days / total_days) * 100
    # Bonus up to 10 pts for logging ≥3 meals
    bonus = min(10, (slot_coverage / 3) * 10)
    score = round(min(100, base + bonus))

    # Trend: compare last 7 days avg vs previous 7 days avg (if enough data)
    trend_delta = None
    if days >= 7:
        prev_start = (today - timedelta(days=days * 2 - 1)).isoformat()
        prev_end = (today - timedelta(days=days)).isoformat()
        prev_pipeline = [
            {"$match": {"user_id": user_id, "date": {"$gte": prev_start, "$lte": prev_end}}},
            {"$group": {"_id": None, "total_kcal": {"$sum": "$totals.calories_kcal"}, "count": {"$sum": 1}}},
        ]
        prev_docs = await db.food_logs.aggregate(prev_pipeline).to_list(None)
        if prev_docs and prev_docs[0].get("count", 0) > 0:
            prev_avg = prev_docs[0]["total_kcal"] / prev_docs[0]["count"]
            if avg_kcal is not None:
                trend_delta = round(avg_kcal - prev_avg)

    return {
        "score": score,
        "logged_days": logged_days,
        "total_days": total_days,
        "avg_kcal_7d": round(avg_kcal) if avg_kcal is not None else None,
        "trend_delta_kcal": trend_delta,
        "slot_coverage_avg": round(slot_coverage, 1),
    }
