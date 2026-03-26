# app/tasks/advisory_tasks.py

import logging
import uuid
from datetime import datetime, timedelta
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session
from app.tasks.celery_app import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)

sync_engine = create_engine(
    settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")
)


@celery_app.task(bind=True, max_retries=3, queue="advisory")
def process_advisory_request(self, advisory_request_id: str):
    """
    Process one advisory request end-to-end.

    Status machine:
        pending → processing → completed  (image_b64 cleared here + by DB trigger)
                             → failed     (image_b64 cleared here + by DB trigger)

    image_b64 is cleared in the app layer AND the DB trigger fires as a
    belt-and-suspenders. Either one alone is sufficient; both together means
    the blob is guaranteed gone regardless of which code path ran.
    """
    from app.models.advisory import AdvisoryRequest, AdvisoryStatus
    from app.models.farmer import Farmer
    from app.models.field import Field
    from app.services.advisory_agent import run_advisory_pipeline

    try:
        logger.info(
            f"Processing advisory {advisory_request_id} "
            f"(attempt {self.request.retries + 1})"
        )

        with Session(sync_engine) as db:
            advisory = db.execute(
                select(AdvisoryRequest).where(
                    AdvisoryRequest.id == uuid.UUID(advisory_request_id)
                )
            ).scalar_one_or_none()

            if not advisory:
                logger.error(f"Advisory {advisory_request_id} not found")
                return

            if advisory.status == AdvisoryStatus.completed:
                logger.info(f"Advisory {advisory_request_id} already completed")
                return

            advisory.status = AdvisoryStatus.processing
            db.commit()

        with Session(sync_engine) as db:
            advisory = db.execute(
                select(AdvisoryRequest).where(
                    AdvisoryRequest.id == uuid.UUID(advisory_request_id)
                )
            ).scalar_one_or_none()

            farmer = db.execute(
                select(Farmer).where(
                    Farmer.phone_number == advisory.farmer_phone
                )
            ).scalar_one_or_none()

            field = db.execute(
                select(Field)
                .where(
                    Field.farmer_phone == advisory.farmer_phone,
                    Field.is_active == True,
                )
                .order_by(Field.created_at.desc())
            ).scalar_one_or_none()

            response = run_advisory_pipeline(
                request_id=advisory_request_id,
                farmer_phone=advisory.farmer_phone,
                query_text=advisory.query_text,
                image_url=advisory.image_url,
                image_b64=advisory.image_b64,
                image_media_type=advisory.image_media_type,
                farmer_name=farmer.name if farmer else None,
                crop_type=field.crop_type if field else None,
                area_ha=field.area_ha if field else None,
                soil_type=field.soil_type if field else None,
                irrigation_method=field.irrigation_method if field else None,
                district=farmer.district if farmer else None,
                ndvi_score=field.ndvi_score if field else None,
            )

            advisory.status       = AdvisoryStatus.completed
            advisory.response_text = response
            advisory.image_b64    = None   # app-layer clear (DB trigger also fires)
            db.commit()

            if advisory.channel == "whatsapp":
                _send_whatsapp_reply(advisory.farmer_phone, response)

            logger.info(f"Completed advisory {advisory_request_id}")
            return {"status": "completed", "request_id": advisory_request_id}

    except Exception as exc:
        logger.error(
            f"Advisory task failed "
            f"(attempt {self.request.retries + 1}/{self.max_retries + 1}): {exc}"
        )

        is_final_attempt = self.request.retries >= self.max_retries

        if is_final_attempt:
            try:
                with Session(sync_engine) as db:
                    adv = db.execute(
                        select(AdvisoryRequest).where(
                            AdvisoryRequest.id == uuid.UUID(advisory_request_id)
                        )
                    ).scalar_one_or_none()
                    if adv:
                        adv.status    = AdvisoryStatus.failed
                        adv.image_b64 = None  # clear blob on failure too
                        db.commit()
                        logger.error(
                            f"Advisory {advisory_request_id} permanently failed "
                            f"after {self.max_retries + 1} attempts"
                        )
            except Exception as db_exc:
                logger.error(f"Failed to mark advisory as failed: {db_exc}")
        else:
            countdown = 30 * (2 ** self.request.retries)
            logger.info(
                f"Retrying advisory {advisory_request_id} in {countdown}s"
            )
            raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=2)
def cleanup_stuck_advisories(self):
    """
    Runs every 10 minutes via Celery Beat.
    Finds advisory requests stuck in 'processing' for > 5 minutes
    and marks them failed. Clears their image_b64 at the same time.
    """
    from app.models.advisory import AdvisoryRequest, AdvisoryStatus

    cutoff = datetime.utcnow() - timedelta(minutes=5)

    try:
        with Session(sync_engine) as db:
            stuck = db.execute(
                select(AdvisoryRequest).where(
                    AdvisoryRequest.status == AdvisoryStatus.processing,
                    AdvisoryRequest.updated_at < cutoff,
                )
            ).scalars().all()

            if not stuck:
                return {"stuck_found": 0}

            for adv in stuck:
                adv.status    = AdvisoryStatus.failed
                adv.image_b64 = None
                logger.warning(
                    f"Marked stuck advisory {adv.id} as failed, blob cleared"
                )

            db.commit()
            logger.info(f"Cleaned up {len(stuck)} stuck advisory requests")
            return {"stuck_found": len(stuck)}

    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@celery_app.task
def purge_stale_image_blobs():
    """
    Runs once daily via Celery Beat.

    Safety net — finds any advisory_requests row that:
      - Still has image_b64 set (blob not cleared)
      - Was last updated more than 24 hours ago

    This catches rows that slipped through the trigger and task-level
    clears — e.g. if the DB was modified directly, or a migration
    was applied to an already-running instance.

    Uses a bulk UPDATE for efficiency — no Python loop needed.
    The partial index ix_advisory_b64_cleanup makes this fast even
    on large tables because it only indexes rows with image_b64 IS NOT NULL.
    """
    from app.models.advisory import AdvisoryRequest

    cutoff = datetime.utcnow() - timedelta(hours=24)

    try:
        with Session(sync_engine) as db:
            result = db.execute(
                update(AdvisoryRequest)
                .where(
                    AdvisoryRequest.image_b64.isnot(None),
                    AdvisoryRequest.updated_at < cutoff,
                )
                .values(image_b64=None)
            )
            db.commit()

            cleared = result.rowcount
            if cleared:
                logger.info(
                    f"purge_stale_image_blobs: cleared {cleared} stale blob(s)"
                )
            return {"cleared": cleared}

    except Exception as exc:
        logger.error(f"purge_stale_image_blobs failed: {exc}")
        raise


def _send_whatsapp_reply(phone: str, text: str):
    import asyncio
    from app.services.whatsapp import send_whatsapp_message
    try:
        asyncio.run(send_whatsapp_message(phone, text))
    except Exception as e:
        logger.error(f"WhatsApp reply failed for {phone}: {e}")