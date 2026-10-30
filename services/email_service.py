"""Gmail SMTP OTP email sender."""
import os
import logging
from email.mime.text import MIMEText
from email.utils import formataddr

import aiosmtplib

logger = logging.getLogger(__name__)

_enabled = os.environ.get("EMAIL_ENABLED", "false").lower() == "true"
_from = os.environ.get("EMAIL_FROM", "")
_host = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
_port = int(os.environ.get("EMAIL_PORT", "587"))
_username = os.environ.get("EMAIL_USERNAME", "")
_password = os.environ.get("EMAIL_PASSWORD", "")


async def send_otp(to_email: str, otp: str) -> None:
    if not _enabled:
        logger.info("Email disabled — OTP for %s: %s", to_email, otp)
        return

    # Plain text only: a bare, short message is the least likely to be filtered as spam.
    msg = MIMEText(
        f"Your Snapfit AI verification code is {otp}\n\n"
        f"It expires in 5 minutes. If you didn't request it, ignore this email.\n",
        "plain",
    )
    msg["Subject"] = "Your Snapfit AI verification code"
    msg["From"] = formataddr(("Snapfit AI", _from))
    msg["To"] = to_email

    await aiosmtplib.send(
        msg,
        hostname=_host,
        port=_port,
        username=_username,
        password=_password,
        start_tls=True,
    )
    logger.info("OTP email sent to %s", to_email)
