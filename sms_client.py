"""
Twilio SMS wrapper for GreenGuard USA.

Env vars:
  TWILIO_ACCOUNT_SID  — from console.twilio.com
  TWILIO_AUTH_TOKEN   — from console.twilio.com
  TWILIO_FROM_NUMBER  — your Twilio phone number (e.g. +15125550001)
  ADMIN_SMS_NUMBER    — Dan's cell, receives payment failure alerts
"""

import os
import re
from dotenv import load_dotenv

load_dotenv()

_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
_FROM  = os.getenv("TWILIO_FROM_NUMBER", "")
ADMIN_SMS = os.getenv("ADMIN_SMS_NUMBER", "")


def _normalize(phone: str) -> str | None:
    """Strip formatting, ensure E.164 (+1XXXXXXXXXX). Returns None if can't normalize."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def send_sms(to: str, body: str) -> bool:
    """
    Send an SMS via Twilio. Returns True on success, False on failure.
    Silently skips if Twilio is not configured.
    """
    if not (_SID and _TOKEN and _FROM):
        return False

    to_e164 = _normalize(to)
    if not to_e164:
        return False

    try:
        from twilio.rest import Client
        client = Client(_SID, _TOKEN)
        client.messages.create(body=body, from_=_FROM, to=to_e164)
        return True
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("SMS failed to %s: %s", to, exc)
        return False


def sms_configured() -> bool:
    return bool(_SID and _TOKEN and _FROM)
