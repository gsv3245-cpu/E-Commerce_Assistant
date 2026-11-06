"""SMTP delivery for one-time sign-in codes."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import config


def smtp_is_configured() -> bool:
    """Return whether the required SMTP credentials and sender are present."""
    return bool(
        config.SMTP_HOST
        and config.SMTP_PORT
        and config.SMTP_USERNAME
        and config.SMTP_PASSWORD.strip()
        and config.SMTP_FROM
    )


def send_otp_email(recipient: str, code: str) -> None:
    """Send a plain-text OTP over authenticated SMTP using TLS."""
    if not smtp_is_configured():
        raise RuntimeError("SMTP settings are incomplete")

    message = EmailMessage()
    message["Subject"] = f"Your {config.STORE_NAME} sign-in code"
    message["From"] = formataddr((config.STORE_NAME, config.SMTP_FROM))
    message["To"] = recipient
    message.set_content(
        f"Your {config.STORE_NAME} sign-in code is {code}.\n\n"
        f"It expires in {config.OTP_TTL_SECONDS // 60} minutes. "
        "If you did not request this code, you can ignore this email."
    )

    # Gmail shows App Passwords with spaces for readability; SMTP expects the
    # same credential without those spaces.
    password = "".join(config.SMTP_PASSWORD.split())
    tls_context = ssl.create_default_context()
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                      timeout=config.SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.ehlo()
        smtp.starttls(context=tls_context)
        smtp.ehlo()
        smtp.login(config.SMTP_USERNAME, password)
        smtp.send_message(message)
