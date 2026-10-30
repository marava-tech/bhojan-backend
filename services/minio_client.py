"""Image upload via s3.marava.tech."""
import io
import logging
import os
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

UPLOAD_URL = os.environ["UPLOAD_URL"]
UPLOAD_API_KEY = os.environ["UPLOAD_API_KEY"]
BUCKET = "ai-calorie-counter"

BUCKET_FOOD = BUCKET
BUCKET_GYM = BUCKET
BUCKET_BOWL = BUCKET
BUCKET_PROFILE = BUCKET


async def download_image(url: str) -> bytes:
    """Fetch image bytes from a public URL."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def upload_image(image_bytes: bytes, bucket: str, filename: str) -> str:
    """Upload image bytes to s3.marava.tech; returns public URL."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            UPLOAD_URL,
            headers={"x-api-key": UPLOAD_API_KEY},
            files={"file": (filename, io.BytesIO(image_bytes), "image/jpeg")},
            data={"bucket": bucket},
        )
        resp.raise_for_status()
        return resp.json()["url"]


async def delete_image(url: str) -> None:
    """Best-effort delete of an object by its public URL (<host>/<bucket>/<key>). Never raises."""
    try:
        bucket, _, key = urlparse(url).path.lstrip("/").partition("/")
        if not key:
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                "DELETE", UPLOAD_URL, headers={"x-api-key": UPLOAD_API_KEY}, json={"bucket": bucket, "key": key}
            )
            resp.raise_for_status()
    except Exception:
        logger.warning("Could not delete image %s", url, exc_info=True)
