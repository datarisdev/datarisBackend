from celery import Celery
from celery.schedules import crontab
import os
# FORCE MODEL REGISTRATION HERE
import app.models  # noqa: F401 

REDIS_URL = os.getenv("REDIS_URL", "redis://default:dJhuLhWWu3qZeK2Ir6x9fGyhYD1dCdpn@redis-11931.c328.europe-west3-1.gce.cloud.redislabs.com:11931/0")

celery_app = Celery(
    "satellite_jobs",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# -----------------------------
# Celery Beat (scheduler)
# -----------------------------
celery_app.conf.beat_schedule = {
    "daily-sentinel-job-scheduler": {
        "task": "app.api.task.schedule_daily_satellite_jobs",
        "schedule": crontab(hour=3, minute=0),  # 03:00 UTC
    },
}

# Import tasks here so Celery registers them
import app.api.task
#import app.api.task_weather 
#celery -A app.celery_app.celery_app worker --loglevel=info --pool=solo