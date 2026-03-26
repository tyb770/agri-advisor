# app/tasks/celery_app.py

from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "agri_advisory",
    broker="redis://localhost:6379/1",
    backend="redis://localhost:6379/2",
    include=[
        "app.tasks.advisory_tasks",
        "app.tasks.satellite_tasks",
        "app.tasks.report_tasks",
    ]
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Karachi",
    enable_utc=True,

    task_routes={
        "app.tasks.advisory_tasks.process_advisory_request":    {"queue": "advisory"},
        "app.tasks.advisory_tasks.cleanup_stuck_advisories":    {"queue": "default"},
        "app.tasks.advisory_tasks.purge_stale_image_blobs":     {"queue": "default"},
        "app.tasks.satellite_tasks.update_field_health_snapshots": {"queue": "default"},
        "app.tasks.report_tasks.generate_weekly_farmer_reports":   {"queue": "default"},
    },

    beat_schedule={
        "nightly-field-health-update": {
            "task": "app.tasks.satellite_tasks.update_field_health_snapshots",
            "schedule": crontab(hour=1, minute=0),
        },
        "weekly-farmer-reports": {
            "task": "app.tasks.report_tasks.generate_weekly_farmer_reports",
            "schedule": crontab(hour=8, minute=0, day_of_week=0),
        },
        "cleanup-stuck-advisories": {
            "task": "app.tasks.advisory_tasks.cleanup_stuck_advisories",
            "schedule": crontab(minute="*/10"),
        },
        # Daily safety net — clears any image_b64 blobs older than 24h
        "purge-stale-image-blobs": {
            "task": "app.tasks.advisory_tasks.purge_stale_image_blobs",
            "schedule": crontab(hour=3, minute=0),  # 3:00 AM Karachi, low traffic
        },
    },

    task_soft_time_limit=60,
    task_time_limit=90,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)