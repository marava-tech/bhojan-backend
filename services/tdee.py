"""TDEE calculation — Mifflin-St Jeor × 1.2 (desk-job baseline) + gym sessions priced by MET.

The old Harris-Benedict multiplier table (up to 1.9 for daily training) overshot a WHOOP-measured
user by 59% (3010 vs 1888 kcal/day); see scripts/check_tdee_with_data.py. This is only the starting
guess — the adaptive TDEE (services/adaptive_tdee.py) replaces it once intake + weight exist."""

RECOMP_SURPLUS = 75     # kcal above TDEE
PROTEIN_RATIO = 2.0     # g per kg body weight


# Net cost of one ~1h gym session: (MET 4 − 1 resting) × kg × 1h. A WHOOP-tracked 64 kg user
# measured ~140 kcal gross per strength session, so this is already on the generous side.
_SESSION_NET_MET_HOURS = 3.0


def _training_kcal_per_day(weight_kg: float, gym_days_per_week: int) -> float:
    return gym_days_per_week * _SESSION_NET_MET_HOURS * weight_kg / 7


def calculate_tdee(
    weight_kg: float,
    height_cm: float,
    age: int,
    sex: str,
    gym_days_per_week: int = 5,
) -> dict:
    # Mifflin-St Jeor BMR
    if sex.lower() == "male":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    tdee = round(bmr * 1.2 + _training_kcal_per_day(weight_kg, gym_days_per_week))
    goal_kcal = tdee + RECOMP_SURPLUS
    protein_g = round(weight_kg * PROTEIN_RATIO)

    # Remaining kcal split roughly 25% fat / rest carbs
    fat_g = round(goal_kcal * 0.25 / 9)
    carbs_g = round((goal_kcal - protein_g * 4 - fat_g * 9) / 4)

    return {
        "tdee_kcal": tdee,
        "goal_kcal": goal_kcal,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
    }
