# app/services/onboarding.py
"""
WhatsApp onboarding state machine.

Redis key: whatsapp:session:{phone}
Value (JSON):
{
  "state": "onboarding",
  "step":  one of STEPS,
  "data":  { collected fields so far }
}

Step order:
  ask_name → ask_district → ask_village → ask_crop →
  ask_area → ask_soil → ask_irrigation → ask_language →
  ask_location → done

ask_location expects either:
  - A WhatsApp "location" message  (lat/lng populated from payload)
  - The word "skip"                (lat/lng stay null)

After "done": Farmer + Field written to Postgres, session deleted.
"""

import json
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.farmer import Farmer
from app.models.field import Field

logger = logging.getLogger(__name__)

SESSION_TTL = 60 * 30   # 30 minutes of inactivity → session expires


# ── Redis helpers ─────────────────────────────────────────────

def _key(phone: str) -> str:
    return f"whatsapp:session:{phone}"


async def get_session(redis, phone: str) -> dict | None:
    raw = await redis.get(_key(phone))
    return json.loads(raw) if raw else None


async def save_session(redis, phone: str, session: dict):
    await redis.setex(_key(phone), SESSION_TTL, json.dumps(session))


async def delete_session(redis, phone: str):
    await redis.delete(_key(phone))


async def is_farmer_registered(db: AsyncSession, phone: str) -> bool:
    result = await db.execute(
        select(Farmer).where(Farmer.phone_number == phone, Farmer.is_active == True)
    )
    return result.scalar_one_or_none() is not None


# ── Step definitions ──────────────────────────────────────────

STEPS = [
    "ask_name",
    "ask_district",
    "ask_village",
    "ask_crop",
    "ask_area",
    "ask_soil",
    "ask_irrigation",
    "ask_language",
    "ask_location",
]

QUESTIONS = {
    "ask_name": (
        "🌾 *زرعی ایڈوائزری سسٹم میں خوش آمدید!*\n"
        "Welcome to Agri Advisory System — Punjab\n\n"
        "آپ کا نام کیا ہے؟\n"
        "_What is your name?_"
    ),
    "ask_district": (
        "آپ کا ضلع کیا ہے؟\n"
        "_What is your district?_\n\n"
        "مثال: Faisalabad, Lahore, Multan, Gujranwala, Sialkot"
    ),
    "ask_village": (
        "آپ کا گاؤں یا قصبہ؟\n"
        "_Your village or town?_\n\n"
        "_(چھوڑنے کے لیے 'skip' لکھیں / type 'skip' to skip)_"
    ),
    "ask_crop": (
        "آپ کی مرکزی فصل کیا ہے؟\n"
        "_What is your main crop?_\n\n"
        "1️⃣  گندم — Wheat\n"
        "2️⃣  کپاس — Cotton\n"
        "3️⃣  چاول — Rice\n"
        "4️⃣  گنا — Sugarcane\n"
        "5️⃣  مکئی — Maize\n"
        "6️⃣  دیگر — Other\n\n"
        "_نمبر یا نام لکھیں / type number or name_"
    ),
    "ask_area": (
        "کھیت کا رقبہ ہیکٹر میں؟\n"
        "_Field area in hectares?_\n\n"
        "مثال / Example: 2.5\n"
        "_(چھوڑنے کے لیے 'skip' لکھیں)_"
    ),
    "ask_soil": (
        "مٹی کی قسم؟ / _Soil type?_\n\n"
        "1️⃣  چکنی مٹی — Clay\n"
        "2️⃣  میرا — Loam\n"
        "3️⃣  ریتلی — Sandy\n"
        "4️⃣  دومٹ — Silt\n"
        "5️⃣  معلوم نہیں — Skip\n\n"
        "_نمبر لکھیں / type number_"
    ),
    "ask_irrigation": (
        "آبپاشی کا طریقہ؟ / _Irrigation method?_\n\n"
        "1️⃣  ڈرپ — Drip\n"
        "2️⃣  فلڈ — Flood\n"
        "3️⃣  بارش پر — Rain-fed\n"
        "4️⃣  اسپرنکلر — Sprinkler\n"
        "5️⃣  معلوم نہیں — Skip\n\n"
        "_نمبر لکھیں / type number_"
    ),
    "ask_language": (
        "مشورے کے لیے زبان؟ / _Preferred language for advice?_\n\n"
        "1️⃣  اردو — Urdu\n"
        "2️⃣  پنجابی — Punjabi\n"
        "3️⃣  انگریزی — English\n\n"
        "_نمبر لکھیں / type number_"
    ),
    "ask_location": (
        "آخری قدم! 🎯\n\n"
        "*اپنے کھیت میں کھڑے ہو کر* اپنی live location بھیجیں:\n\n"
        "📎 *Attach* → *Location* → *Send Your Current Location*\n\n"
        "_Last step! Stand in your field and share your live location:_\n"
        "_Tap 📎 → Location → Send Your Current Location_\n\n"
        "_(چھوڑنے کے لیے 'skip' لکھیں / type 'skip' to skip)_"
    ),
}

# ── Input lookup tables ───────────────────────────────────────

CROP_MAP = {
    "1": "wheat",     "wheat": "wheat",         "گندم": "wheat",
    "2": "cotton",    "cotton": "cotton",        "کپاس": "cotton",
    "3": "rice",      "rice": "rice",            "چاول": "rice",
    "4": "sugarcane", "sugarcane": "sugarcane",  "گنا": "sugarcane",
    "5": "maize",     "maize": "maize",          "مکئی": "maize",
    "6": "other",     "other": "other",          "دیگر": "other",
}

SOIL_MAP = {
    "1": "clay",  "clay": "clay",   "چکنی": "clay", "چکنی مٹی": "clay",
    "2": "loam",  "loam": "loam",   "میرا": "loam",
    "3": "sandy", "sandy": "sandy", "ریتلی": "sandy",
    "4": "silt",  "silt": "silt",   "دومٹ": "silt",
    "5": "skip",  "skip": "skip",   "معلوم نہیں": "skip",
}

IRRIGATION_MAP = {
    "1": "drip",      "drip": "drip",          "ڈرپ": "drip",
    "2": "flood",     "flood": "flood",         "فلڈ": "flood",
    "3": "rain-fed",  "rain-fed": "rain-fed",   "بارش": "rain-fed", "rain": "rain-fed",
    "4": "sprinkler", "sprinkler": "sprinkler", "اسپرنکلر": "sprinkler",
    "5": "skip",      "skip": "skip",           "معلوم نہیں": "skip",
}

LANGUAGE_MAP = {
    "1": "ur", "urdu": "ur",    "اردو": "ur",
    "2": "pa", "punjabi": "pa", "پنجابی": "pa",
    "3": "en", "english": "en", "انگریزی": "en",
}

SKIP_WORDS = {"skip", "سکپ", "چھوڑیں", ""}


def _next_step(current: str) -> str | None:
    try:
        idx = STEPS.index(current)
        return STEPS[idx + 1] if idx + 1 < len(STEPS) else None
    except ValueError:
        return None


# ── Main handler ──────────────────────────────────────────────

async def handle_onboarding(
    phone: str,
    text: str,
    redis,
    db: AsyncSession,
    latitude: float | None = None,
    longitude: float | None = None,
    msg_type: str = "text",
) -> tuple[str, bool]:
    """
    Handle one incoming WhatsApp message during onboarding.

    Parameters
    ----------
    phone     : farmer's WhatsApp number e.g. "+923001234567"
    text      : message body text (None for pure location messages)
    redis     : async Redis client (from get_redis dependency)
    db        : async SQLAlchemy session (from get_db dependency)
    latitude  : populated when msg_type == "location"
    longitude : populated when msg_type == "location"
    msg_type  : "text" | "image" | "location"

    Returns
    -------
    (reply_text, is_complete)
    is_complete=True  → farmer is now registered; webhook must NOT
                        route this message into the advisory pipeline.
    """
    session = await get_session(redis, phone)

    # Very first message from this number — start onboarding
    if session is None:
        session = {"state": "onboarding", "step": "ask_name", "data": {}}
        await save_session(redis, phone, session)
        return QUESTIONS["ask_name"], False

    step  = session["step"]
    data  = session["data"]
    t     = (text or "").strip()
    t_low = t.lower()

    # ── Parse answer for current step ─────────────────────────

    if step == "ask_name":
        if not t:
            return "براہ کرم اپنا نام لکھیں۔\n_Please enter your name._", False
        data["name"] = t

    elif step == "ask_district":
        if not t:
            return "براہ کرم اپنا ضلع لکھیں۔\n_Please enter your district._", False
        data["district"] = t

    elif step == "ask_village":
        data["village"] = None if t_low in SKIP_WORDS else t

    elif step == "ask_crop":
        crop = CROP_MAP.get(t_low)
        if not crop:
            return (
                "براہ کرم درست نمبر یا نام لکھیں۔\n"
                "_Please enter a valid number or crop name._\n\n"
                + QUESTIONS["ask_crop"]
            ), False
        data["crop_type"] = crop

    elif step == "ask_area":
        if t_low in SKIP_WORDS:
            data["area_ha"] = None
        else:
            try:
                data["area_ha"] = float(t)
            except ValueError:
                return (
                    "براہ کرم ہیکٹر میں رقبہ لکھیں یا 'skip' لکھیں۔\n"
                    "_Please enter area in hectares (e.g. 2.5) or type 'skip'._"
                ), False

    elif step == "ask_soil":
        val = SOIL_MAP.get(t_low)
        if val is None:
            return (
                "براہ کرم 1 سے 5 نمبر لکھیں۔\n_Please type a number 1–5._\n\n"
                + QUESTIONS["ask_soil"]
            ), False
        data["soil_type"] = None if val == "skip" else val

    elif step == "ask_irrigation":
        val = IRRIGATION_MAP.get(t_low)
        if val is None:
            return (
                "براہ کرم 1 سے 5 نمبر لکھیں۔\n_Please type a number 1–5._\n\n"
                + QUESTIONS["ask_irrigation"]
            ), False
        data["irrigation_method"] = None if val == "skip" else val

    elif step == "ask_language":
        lang = LANGUAGE_MAP.get(t_low, "ur")
        data["preferred_language"] = lang

    elif step == "ask_location":
        if msg_type == "location" and latitude is not None and longitude is not None:
            # ✅ Farmer shared WhatsApp Live Location
            data["latitude"]  = latitude
            data["longitude"] = longitude
            logger.info(
                f"Location received for {phone}: lat={latitude}, lng={longitude}"
            )
        elif t_low in SKIP_WORDS:
            # Farmer explicitly skipped
            data["latitude"]  = None
            data["longitude"] = None
        else:
            # Farmer sent text or image instead of location — remind them
            return (
                "براہ کرم *location* بھیجیں یا 'skip' لکھیں۔\n\n"
                "📎 → *Location* → *Send Your Current Location*\n\n"
                "_Please share your live location or type 'skip'._"
            ), False

    # ── Advance to next step or finish ────────────────────────
    nxt = _next_step(step)

    if nxt is None:
        # All steps done — persist to DB
        try:
            await _persist_farmer(phone, data, db)
            await delete_session(redis, phone)

            name     = data.get("name", "کسان")
            crop     = data.get("crop_type", "")
            loc_note = (
                "📍 کھیت کی location محفوظ ہو گئی۔ / _Field location saved._\n\n"
                if data.get("latitude") else ""
            )
            reply = (
                f"✅ *مبارک ہو {name} صاحب! رجسٹریشن مکمل ہو گئی۔*\n\n"
                f"{loc_note}"
                f"Congratulations! You are now registered.\n\n"
                f"اب اپنی *{crop}* فصل کی تصویر بھیجیں یا کوئی سوال پوچھیں "
                f"— ہمارا AI فوری مشورہ دے گا۔ 🌾\n\n"
                f"Send a *photo* of your crop or ask any question "
                f"and our AI advisor will respond instantly."
            )
            return reply, True

        except Exception as e:
            logger.error(f"Onboarding DB write failed for {phone}: {e}")
            await delete_session(redis, phone)
            return (
                "معذرت، رجسٹریشن میں خرابی آئی۔ دوبارہ کوئی پیغام بھیجیں۔\n"
                "_Sorry, registration failed. Please send any message to try again._"
            ), False
    else:
        session["step"] = nxt
        session["data"] = data
        await save_session(redis, phone, session)
        return QUESTIONS[nxt], False


# ── DB write ──────────────────────────────────────────────────

async def _persist_farmer(phone: str, data: dict, db: AsyncSession):
    """
    Write Farmer + Field rows.
    Idempotent on Farmer to handle rare retry/race conditions.
    """
    existing = await db.execute(
        select(Farmer).where(Farmer.phone_number == phone)
    )
    farmer = existing.scalar_one_or_none()

    if not farmer:
        farmer = Farmer(
            phone_number=phone,
            name=data.get("name", "Unknown"),
            district=data.get("district"),
            village=data.get("village"),
            preferred_language=data.get("preferred_language", "ur"),
            crop_profile={
                "crops":   [data.get("crop_type", "other")],
                "area_ha": data.get("area_ha"),
            },
            is_active=True,
        )
        db.add(farmer)
        await db.flush()   # flush to get PK before creating field FK

    field = Field(
        farmer_phone=phone,
        name="Main Field",
        crop_type=data.get("crop_type", "other"),
        area_ha=data.get("area_ha"),
        soil_type=data.get("soil_type"),
        irrigation_method=data.get("irrigation_method"),
        latitude=data.get("latitude"),    # None if skipped
        longitude=data.get("longitude"),  # None if skipped
        is_active=True,
    )
    db.add(field)
    await db.commit()

    logger.info(
        f"Farmer registered via WhatsApp: {phone} | "
        f"name={data.get('name')} crop={data.get('crop_type')} "
        f"lat={data.get('latitude')} lng={data.get('longitude')}"
    )