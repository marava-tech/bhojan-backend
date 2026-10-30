"""Gmail SMTP OTP email sender."""
import os
import logging
from email.message import EmailMessage
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
        f"Your Bhojan verification code is {otp}\n\n"
        f"It expires in 5 minutes. If you didn't request it, ignore this email.\n",
        "plain",
    )
    msg["Subject"] = "Your Bhojan verification code"
    msg["From"] = formataddr(("Bhojan", _from))
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


async def send_export(to_email: str, files: dict[str, bytes]) -> None:
    """Email the user a copy of their data. `files` maps filename -> bytes."""
    if not _enabled:
        logger.info("Email disabled — data export for %s: %s", to_email, list(files))
        return

    msg = EmailMessage()
    msg["Subject"] = "Your Bhojan data export"
    msg["From"] = formataddr(("Bhojan", _from))
    msg["To"] = to_email
    msg.set_content(
        "Attached is a copy of the data in your Bhojan account: your profile, food diary, "
        "supplements, sleep and weight logs.\n\n"
        "food_logs.csv opens in any spreadsheet app. bhojan_data.json has everything.\n"
        "If you didn't request this, you can ignore this email.\n"
    )
    for name, data in files.items():
        maintype, subtype = ("text", "csv") if name.endswith(".csv") else ("application", "json")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    await aiosmtplib.send(
        msg, hostname=_host, port=_port, username=_username, password=_password, start_tls=True,
    )
    logger.info("Data export emailed to %s", to_email)
