"""Common supplements with a typical serving and its macros, so adding one is a tap, not a form.

Values are per ONE serving (calories_per_unit etc. are multiplied by units taken). Taken from typical
Indian-market labels; the user can edit anything after adding. Kept on the server so the list can grow
without an app release."""

PRESETS = [
    {"key": "whey", "name": "Whey protein", "dose_amount": 1, "dose_unit": "scoop", "timing": "post-workout",
     "calories_per_unit": 120, "protein_per_unit": 24, "carbs_per_unit": 3, "fat_per_unit": 1.5},
    {"key": "creatine", "name": "Creatine", "dose_amount": 5, "dose_unit": "g", "timing": "any",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "multivitamin", "name": "Multivitamin", "dose_amount": 1, "dose_unit": "tablet", "timing": "morning",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "omega3", "name": "Fish oil (omega-3)", "dose_amount": 1, "dose_unit": "capsule", "timing": "with a meal",
     "calories_per_unit": 10, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 1},
    {"key": "d3", "name": "Vitamin D3", "dose_amount": 1, "dose_unit": "capsule", "timing": "morning",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "b12", "name": "Vitamin B12", "dose_amount": 1, "dose_unit": "tablet", "timing": "morning",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "magnesium", "name": "Magnesium", "dose_amount": 1, "dose_unit": "tablet", "timing": "night",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "ashwagandha", "name": "Ashwagandha", "dose_amount": 1, "dose_unit": "capsule", "timing": "night",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "calcium", "name": "Calcium", "dose_amount": 1, "dose_unit": "tablet", "timing": "with a meal",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "zinc", "name": "Zinc", "dose_amount": 1, "dose_unit": "tablet", "timing": "with a meal",
     "calories_per_unit": 0, "protein_per_unit": 0, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "bcaa", "name": "BCAA / EAA", "dose_amount": 1, "dose_unit": "scoop", "timing": "workout",
     "calories_per_unit": 20, "protein_per_unit": 5, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "preworkout", "name": "Pre-workout", "dose_amount": 1, "dose_unit": "scoop", "timing": "pre-workout",
     "calories_per_unit": 10, "protein_per_unit": 0, "carbs_per_unit": 2, "fat_per_unit": 0},
    {"key": "collagen", "name": "Collagen", "dose_amount": 1, "dose_unit": "scoop", "timing": "morning",
     "calories_per_unit": 35, "protein_per_unit": 9, "carbs_per_unit": 0, "fat_per_unit": 0},
    {"key": "mass_gainer", "name": "Mass gainer", "dose_amount": 1, "dose_unit": "scoop", "timing": "post-workout",
     "calories_per_unit": 380, "protein_per_unit": 15, "carbs_per_unit": 75, "fat_per_unit": 2},
]
