"""AI scan quota — status."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from services import ai_quota

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/quota")
async def get_quota(user_id: str = Depends(get_current_user)):
    db = get_db()
    profile = await db.user_profile.find_one({"user_id": user_id})
    return await ai_quota.status(db, user_id, profile)
