# app/tasks/satellite_tasks.py

import logging
from datetime import datetime
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.tasks.celery_app import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)

sync_engine = create_engine(
    settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")
)


@celery_app.task(bind=True, max_retries=3)
def update_field_health_snapshots(self):
    """
    Runs nightly via Celery Beat.
    Fetches weather + estimates NDVI for every active field.
    """
    try:
        from app.models.field import Field
        from app.services.satellite import get_weather_for_field, estimate_ndvi

        with Session(sync_engine) as db:
            fields = db.execute(
                select(Field).where(Field.is_active == True)
            ).scalars().all()

            logger.info(f"Updating health for {len(fields)} fields")

            for field in fields:
                if field.latitude is None or field.longitude is None:
                    logger.warning(f"Field {field.id} has no coordinates, skipping")
                    continue

                weather = get_weather_for_field(field.latitude, field.longitude)
                if not weather:
                    logger.warning(f"No weather data for field {field.id}, skipping")
                    continue

                new_ndvi = estimate_ndvi(
                    crop_type=field.crop_type,
                    weather=weather,
                    current_ndvi=field.ndvi_score,
                )

                # Trigger alert if NDVI dropped >15% vs last known value
                if field.ndvi_score and (field.ndvi_score - new_ndvi) >= 0.15:
                    logger.warning(
                        f"NDVI DROP — Field {field.name} ({field.farmer_phone}): "
                        f"{field.ndvi_score:.2f} → {new_ndvi:.2f}"
                    )
                    # Pass farmer's preferred language so the message is localised
                    check_ndvi_alert.delay(
                        str(field.id),
                        new_ndvi,
                        field.ndvi_score,
                        field.farmer_phone,
                        field.name,
                        field.crop_type,
                    )

                field.ndvi_score = new_ndvi
                field.ndvi_updated_at = datetime.utcnow()

                logger.info(
                    f"Field '{field.name}' updated — "
                    f"NDVI: {new_ndvi}, Temp: {weather.get('temperature_c')}°C"
                )

            db.commit()
            logger.info("Field health snapshot update complete")

    except Exception as exc:
        logger.error(f"Satellite task failed: {exc}")
        raise self.retry(exc=exc, countdown=60 * 10)


@celery_app.task
def check_ndvi_alert(
    field_id: str,
    new_ndvi: float,
    old_ndvi: float,
    farmer_phone: str,
    field_name: str,
    crop_type: str,
):
    """
    Triggered when a field's NDVI drops >15%.
    Sends a WhatsApp alert to the farmer in their preferred language.
    """
    logger.warning(
        f"[ALERT] Field {field_id} health dropped: "
        f"{old_ndvi:.2f} → {new_ndvi:.2f}"
    )

    # Determine health label for the message
    if new_ndvi >= 0.6:
        health_label_ur = "اچھی"
        health_label_en = "Good"
    elif new_ndvi >= 0.4:
        health_label_ur = "درمیانی"
        health_label_en = "Moderate"
    else:
        health_label_ur = "خراب"
        health_label_en = "Poor"

    drop_pct = round((old_ndvi - new_ndvi) / old_ndvi * 100)

    message = (
        f"⚠️ *فصل کی صحت میں کمی — {field_name}*\n\n"
        f"آپ کی *{crop_type}* فصل کی صحت میں {drop_pct}% کمی آئی ہے۔\n"
        f"موجودہ صحت: *{health_label_ur}* (NDVI: {new_ndvi:.2f})\n\n"
        f"براہ کرم اپنی فصل کا معائنہ کریں اور تصویر بھیجیں۔\n"
        f"---\n"
        f"⚠️ *Field Health Alert — {field_name}*\n\n"
        f"Your *{crop_type}* crop health has dropped by {drop_pct}%.\n"
        f"Current health: *{health_label_en}* (NDVI: {new_ndvi:.2f})\n\n"
        f"Please inspect your crop and send a photo for an AI diagnosis."
    )

    _send_whatsapp(farmer_phone, message)


def _send_whatsapp(phone: str, text: str):
    """Sync wrapper around the async WhatsApp sender for Celery tasks."""
    import asyncio
    from app.services.whatsapp import send_whatsapp_message
    try:
        asyncio.run(send_whatsapp_message(phone, text))
        logger.info(f"NDVI alert sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send NDVI alert to {phone}: {e}")