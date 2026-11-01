from pydantic import BaseModel, Field
from typing import Optional


class SavedFoodCreate(BaseModel):
    name: str
    estimated_weight_g: float
    calories_kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    macro_source: str = "ai_estimated"
    image_url: Optional[str] = None
    serving_label: Optional[str] = Field(default=None, max_length=30)  # what 1 saved portion is called: packet, glass, piece
