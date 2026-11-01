"""JWT authentication — OTP-based email auth + get_current_user dependency."""
import os
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId
from bson.errors import InvalidId
from jose import JWTError, jwt

from database import get_db

_jwt_secret = os.environ.get("JWT_SECRET")
if not _jwt_secret:
    raise RuntimeError("JWT_SECRET environment variable is not set")
SECRET_KEY = _jwt_secret
ALGORITHM = "HS256"

# JWT_EXPIRATION in milliseconds (default 15 days)
_expiration_ms = int(os.environ.get("JWT_EXPIRATION", 1296000000))
_expiration_s = _expiration_ms // 1000

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(user_id: str) -> str:
    exp = int(datetime.now(timezone.utc).timestamp()) + _expiration_s
    return jwt.encode({"sub": user_id, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


# A valid token is not enough: the account may have been deleted since it was issued.
# Ids seen alive are trusted for a short while so this is not a query per request.
_KNOWN_TTL_S = 30
_known_users: dict[str, float] = {}


def forget_user(user_id: str) -> None:
    _known_users.pop(user_id, None)


async def _require_user(user_id: str) -> None:
    now = time.monotonic()
    if _known_users.get(user_id, 0) > now:
        return
    try:
        oid = ObjectId(user_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if not await get_db().users.find_one({"_id": oid}, {"_id": 1}):
        _known_users.pop(user_id, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account no longer exists")
    if len(_known_users) > 20000:
        _known_users.clear()
    _known_users[user_id] = now + _KNOWN_TTL_S


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    await _require_user(user_id)
    return user_id
