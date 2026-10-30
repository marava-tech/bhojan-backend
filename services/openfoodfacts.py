"""OpenFoodFacts macro lookup — fallback to Gemini if not found."""
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)


OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"


async def lookup_macros(food_name: str, weight_g: float) -> Optional[dict]:
    """
    Returns macros scaled to weight_g, or None if not found.
    Result: {calories_kcal, protein_g, carbs_g, fat_g, source: "database"}
    """
    params = {
        "search_terms": food_name,
        "json": 1,
        "page_size": 5,
        "fields": "nutriments,product_name",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(OFF_SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("OpenFoodFacts lookup failed for '%s': %s", food_name, e)
        return None

    products = data.get("products", [])
    for product in products:
        n = product.get("nutriments", {})
        cal = n.get("energy-kcal_100g") or n.get("energy_100g", 0)
        protein = n.get("proteins_100g", 0)
        carbs = n.get("carbohydrates_100g", 0)
        fat = n.get("fat_100g", 0)

        if cal and protein is not None:
            ratio = weight_g / 100.0
            return {
                "calories_kcal": round(cal * ratio, 1),
                "protein_g": round(protein * ratio, 1),
                "carbs_g": round(carbs * ratio, 1),
                "fat_g": round(fat * ratio, 1),
                "source": "database",
            }
    return None


OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def lookup_barcode(code: str) -> Optional[dict]:
    """Exact product by barcode → per-100g label values, or None if unknown / no energy value.
    Result: {name, brand, per_100g: {calories_kcal, protein_g, carbs_g, fat_g}, serving_g, image_url}"""
    params = {"fields": "product_name,product_name_en,brands,nutriments,serving_quantity,image_front_small_url"}
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Bhojan/1.0 (tech.marava@gmail.com)"}) as client:
            resp = await client.get(OFF_PRODUCT_URL.format(code=code), params=params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("OpenFoodFacts barcode lookup failed for %s: %s", code, e)
        return None

    product = data.get("product") or {}
    n = product.get("nutriments") or {}
    kcal = _num(n.get("energy-kcal_100g"))
    if kcal is None and _num(n.get("energy_100g")) is not None:
        kcal = _num(n.get("energy_100g")) / 4.184  # energy_100g is kJ
    if not kcal:
        return None
    return {
        "name": (product.get("product_name_en") or product.get("product_name") or "").strip() or f"Product {code}",
        "brand": (product.get("brands") or "").split(",")[0].strip() or None,
        "per_100g": {
            "calories_kcal": round(kcal, 1),
            "protein_g": round(_num(n.get("proteins_100g")) or 0, 1),
            "carbs_g": round(_num(n.get("carbohydrates_100g")) or 0, 1),
            "fat_g": round(_num(n.get("fat_100g")) or 0, 1),
        },
        "serving_g": _num(product.get("serving_quantity")),
        "image_url": product.get("image_front_small_url"),
    }
