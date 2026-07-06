from app.core.satellite_config import SATELLITE_INDICES, SATELLITE_MAX_CLOUD
from app.models.parcel import Parcel
from app.services.satellite.processor import process_sentinel_for_parcel_day
from app.models.satellite_job import SatelliteJob, JobStatus
from app.db.session import SessionLocal
from celery.utils.log import get_task_logger
from datetime import date

from app.modules.ml_training import repository as ml_repository
from app.modules.ml_training.service import refresh_job_status

logger = get_task_logger(__name__)

from app.celery_app import celery_app


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.api.task.process_satellite_day_job",
    acks_late=True,
)
def process_satellite_day_job(self, job_id: str):
    db = SessionLocal()
    try:
        job = db.query(SatelliteJob).filter(SatelliteJob.id == job_id).first()
        if not job:
            logger.warning(f"SatelliteJob {job_id} not found")
            return

        logger.info(f"Processing SatelliteJob {job_id} for {job.day}")

        job.status = JobStatus.RUNNING
        db.commit()

        # Process the day
        results = process_sentinel_for_parcel_day(
            parcel_geometry=job.parcel.geometry,
            user_id=str(job.user_id),
            parcel_id=str(job.parcel_id),
            day=job.day,
            indices=job.indices or ["NDVI"],      # fallback if empty
            max_cloud=job.max_cloud,              # optional
            db_session=db,
        )

        # Update job as DONE
        job.status = JobStatus.DONE
        db.commit()

        logger.info(f"SatelliteJob {job_id} DONE with {len(results)} images processed")

    except Exception as e:
        logger.error(f"SatelliteJob {job_id} FAILED: {e}")
        db.rollback()
        job = db.query(SatelliteJob).filter(SatelliteJob.id == job_id).first()
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.attempts += 1
        db.commit()
        
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60)
    finally:
        db.close()

@celery_app.task(
    name="app.api.task.schedule_daily_satellite_jobs",
    acks_late=True,
)
def schedule_daily_satellite_jobs():
    db = SessionLocal()
    try:
        today = date.today()

        parcels = db.query(Parcel).all()

        created = 0

        for parcel in parcels:
            exists = (
                db.query(SatelliteJob)
                .filter(
                    SatelliteJob.parcel_id == parcel.id,
                    SatelliteJob.day == today,
                )
                .first()
            )

            if exists:
                continue

            job = SatelliteJob(
                user_id=parcel.user_id,
                parcel_id=parcel.id,
                day=today,
                status=JobStatus.PENDING,
                indices=SATELLITE_INDICES,
                max_cloud=SATELLITE_MAX_CLOUD,
            )

            db.add(job)
            db.flush()  # get job.id without commit

            process_satellite_day_job.delay(str(job.id))
            created += 1

        db.commit()

        logger.info(f"Scheduled {created} daily satellite jobs")

    except Exception as e:
        db.rollback()
        logger.error(f"Daily scheduler failed: {e}")
        raise
    finally:
        db.close()


@celery_app.task(
    name="app.api.task.sync_training_job_status",
    acks_late=True,
)
def sync_training_job_status():
    """Reconcilia el estado de los TrainingJob no terminales contra Azure ML.

    Evita que un job quede indefinidamente en running/queued: traduce el
    estado real de Azure ML, y refresh_job_status() marca como EXPIRED
    cualquier job que exceda su timeout_minutes + margen de tolerancia.
    """
    db = SessionLocal()
    try:
        jobs = ml_repository.list_non_terminal_jobs(db)
        for job in jobs:
            try:
                refresh_job_status(db, job)
            except Exception as exc:  # noqa: BLE001 - un job problemático no debe frenar al resto
                logger.error(f"No se pudo sincronizar TrainingJob {job.id}: {exc}")
        logger.info(f"Sincronizados {len(jobs)} training job(s) no terminales")
    finally:
        db.close()
