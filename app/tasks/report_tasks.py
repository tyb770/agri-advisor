# app/tasks/report_tasks.py

import logging
from datetime import datetime, timedelta
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.tasks.celery_app import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)

sync_engine = create_engine(
    settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")
)


@celery_app.task(bind=True, max_retries=3)
def generate_weekly_farmer_reports(self):
    """
    Runs every Sunday at 8am Karachi time.
    Generates and sends a weekly summary to each active farmer via WhatsApp.
    """
    try:
        from app.models.farmer import Farmer

        with Session(sync_engine) as db:
            farmers = db.execute(
                select(Farmer).where(Farmer.is_active == True)
            ).scalars().all()

            logger.info(f"Sending weekly reports to {len(farmers)} farmers")

            sent     = 0
            failed   = 0
            skipped  = 0

            for farmer in farmers:
                try:
                    report = build_farmer_report(db, farmer)

                    # Only send if there's something worth reporting
                    # (skip farmers with no fields AND no advisories this week)
                    if report is None:
                        skipped += 1
                        continue

                    _send_whatsapp(farmer.phone_number, report)
                    sent += 1

                except Exception as e:
                    # One farmer failing shouldn't stop the rest
                    logger.error(
                        f"Failed to send report to {farmer.phone_number}: {e}"
                    )
                    failed += 1
                    continue

            logger.info(
                f"Weekly reports complete — "
                f"sent: {sent}, failed: {failed}, skipped: {skipped}"
            )

    except Exception as exc:
        logger.error(f"Report task failed: {exc}")
        raise self.retry(exc=exc, countdown=60 * 5)


def build_farmer_report(db: Session, farmer) -> str | None:
    """
    Build the weekly report message for one farmer.
    Returns None if the farmer has no fields and no recent advisories
    (no point sending an empty report).
    """
    from app.models.field import Field
    from app.models.advisory import AdvisoryRequest, AdvisoryStatus

    week_ago = datetime.utcnow() - timedelta(days=7)

    fields = db.execute(
        select(Field).where(
            Field.farmer_phone == farmer.phone_number,
            Field.is_active == True,
        )
    ).scalars().all()

    advisories = db.execute(
        select(AdvisoryRequest).where(
            AdvisoryRequest.farmer_phone == farmer.phone_number,
            AdvisoryRequest.created_at >= week_ago,
            AdvisoryRequest.status == AdvisoryStatus.completed,
        )
    ).scalars().all()

    # Nothing to report — skip this farmer
    if not fields and not advisories:
        return None

    lines = []

    # ── Urdu greeting ─────────────────────────────────────────
    lines.append(f"السلام علیکم *{farmer.name}* صاحب!")
    lines.append(
        f"آپ کی ہفتہ وار رپورٹ — "
        f"{datetime.utcnow().strftime('%d %B %Y')}"
    )
    lines.append("")

    # ── Field health section ──────────────────────────────────
    if fields:
        lines.append("📊 *فصل کی صحت (Field Health):*")
        for field in fields:
            ndvi = field.ndvi_score
            if ndvi is None:
                health_ur, health_en = "معلوم نہیں", "Unknown"
                icon = "❓"
            elif ndvi >= 0.6:
                health_ur, health_en = "اچھی", "Good"
                icon = "✅"
            elif ndvi >= 0.4:
                health_ur, health_en = "درمیانی", "Moderate"
                icon = "⚠️"
            else:
                health_ur, health_en = "خراب", "Poor"
                icon = "🔴"

            ndvi_str = f"{ndvi:.2f}" if ndvi else "N/A"
            lines.append(
                f"  • {field.name} ({field.crop_type}): "
                f"{icon} {health_ur} / {health_en} | NDVI: {ndvi_str}"
            )

            # Flag poor health fields urgently
            if ndvi is not None and ndvi < 0.4:
                lines.append(
                    f"    ⚠️ اس کھیت پر فوری توجہ دیں!"
                    f" / This field needs immediate attention!"
                )
    else:
        lines.append("کوئی کھیت رجسٹر نہیں ہے۔ / No fields registered.")

    lines.append("")

    # ── Advisory section ──────────────────────────────────────
    if advisories:
        lines.append(
            f"💬 *اس ہفتے {len(advisories)} مشورے:*"
            f" / *{len(advisories)} advisories this week:*"
        )
        # Show last 3 max — WhatsApp has a 4096 char limit
        for adv in advisories[-3:]:
            date_str = adv.created_at.strftime("%d %b")
            preview  = (adv.query_text or "Photo scan")[:50]
            lines.append(f"  • {date_str}: {preview}...")
    else:
        lines.append(
            "💬 اس ہفتے کوئی سوال نہیں پوچھا گیا۔"
            " / No advisories this week."
        )

    lines.append("")

    # ── Summary stats ─────────────────────────────────────────
    lines.append("---")
    lines.append(f"📍 Fields monitored: {len(fields)}")
    lines.append(f"💬 Advisories received: {len(advisories)}")

    ndvi_scores = [f.ndvi_score for f in fields if f.ndvi_score is not None]
    if ndvi_scores:
        avg = sum(ndvi_scores) / len(ndvi_scores)
        lines.append(f"🌱 Avg field health (NDVI): {avg:.2f}")

    lines.append("")
    lines.append(
        "فصل کی تصویر بھیجیں — AI فوری تشخیص دے گا۔ 🌾\n"
        "Send a crop photo for instant AI diagnosis."
    )
    lines.append("")
    lines.append("_Agri Advisory System — Punjab_")

    report = "\n".join(lines)

    # Trim to WhatsApp's 4096 char hard limit
    if len(report) > 4000:
        report = report[:3997] + "..."

    return report


def _send_whatsapp(phone: str, text: str):
    """Sync wrapper around the async WhatsApp sender for Celery tasks."""
    import asyncio
    from app.services.whatsapp import send_whatsapp_message
    try:
        asyncio.run(send_whatsapp_message(phone, text))
        logger.info(f"Weekly report sent to {phone}")
    except Exception as e:
        logger.error(f"Failed to send weekly report to {phone}: {e}")