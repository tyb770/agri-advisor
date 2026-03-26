# app/services/whatsapp.py

import logging
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)

WHATSAPP_API_URL = (
    f"https://graph.facebook.com/v19.0/"
    f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
)

HEADERS = {
    "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
    "Content-Type": "application/json",
}


async def send_whatsapp_message(to: str, text: str) -> bool:
    """
    Send a text message to a WhatsApp number.
    'to' must be in international format: +923001234567
    Returns True if sent successfully.
    """
    if not settings.WHATSAPP_TOKEN or not settings.WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("WhatsApp credentials not configured — skipping send")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to.replace("+", ""),   # Meta wants no + prefix
        "type": "text",
        "text": {"body": text[:4096]},
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                WHATSAPP_API_URL,
                json=payload,
                headers=HEADERS,
            )
            response.raise_for_status()
            logger.info(f"WhatsApp message sent to {to}")
            return True

    except httpx.HTTPStatusError as e:
        logger.error(f"WhatsApp API error {e.response.status_code}: {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return False


def extract_message_from_payload(payload: dict) -> dict | None:
    """
    Parse the real WhatsApp Cloud API webhook payload.

    Handles three message types:
      - text     : plain text from the farmer
      - image    : photo (with optional caption)
      - location : WhatsApp Live Location or static pin

    Returns a normalised dict:
    {
        "message_id":  str,
        "from_number": str,    # e.g. "+923001234567"
        "type":        "text" | "image" | "location",
        "text":        str | None,
        "image_url":   str | None,   # Graph API URL — needs Bearer auth to fetch
        "latitude":    float | None, # set for type="location" only
        "longitude":   float | None, # set for type="location" only
    }

    Returns None for delivery receipts, read receipts, status updates,
    and unhandled types (audio, video, document, sticker, contacts, etc.)
    """
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None

        changes = entry[0].get("changes", [])
        if not changes:
            return None

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            # delivery receipts / status updates — not a user message
            return None

        msg         = messages[0]
        from_number = "+" + msg.get("from", "")
        message_id  = msg.get("id")
        msg_type    = msg.get("type")

        # ── Text ──────────────────────────────────────────────
        if msg_type == "text":
            return {
                "message_id":  message_id,
                "from_number": from_number,
                "type":        "text",
                "text":        msg.get("text", {}).get("body"),
                "image_url":   None,
                "latitude":    None,
                "longitude":   None,
            }

        # ── Image ─────────────────────────────────────────────
        elif msg_type == "image":
            image_id  = msg.get("image", {}).get("id")
            caption   = msg.get("image", {}).get("caption")   # optional
            # Store as Graph API endpoint; advisory_agent fetches bytes with token
            image_url = f"https://graph.facebook.com/v19.0/{image_id}"
            return {
                "message_id":  message_id,
                "from_number": from_number,
                "type":        "image",
                "text":        caption,
                "image_url":   image_url,
                "latitude":    None,
                "longitude":   None,
            }

        # ── Location (Live Location or static pin) ────────────
        elif msg_type == "location":
            loc = msg.get("location", {})
            lat = loc.get("latitude")
            lng = loc.get("longitude")
            if lat is None or lng is None:
                logger.warning(f"Location message missing lat/lng from {from_number}")
                return None
            return {
                "message_id":  message_id,
                "from_number": from_number,
                "type":        "location",
                "text":        None,
                "image_url":   None,
                "latitude":    float(lat),
                "longitude":   float(lng),
            }

        # ── Unsupported types ─────────────────────────────────
        else:
            logger.info(
                f"Unhandled WhatsApp message type '{msg_type}' "
                f"from {from_number} — ignoring"
            )
            return None

    except Exception as e:
        logger.error(f"Failed to parse WhatsApp payload: {e}")
        return None