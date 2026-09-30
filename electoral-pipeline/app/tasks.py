import logging

from celery import Celery

from app import config
from app.workflow import discover, dispatch_documents, dispatch_units, process_document_job, process_unit_job, recover


log = logging.getLogger(__name__)

log.info(f"[TASKS] Initializing Celery with broker={config.REDIS_URL}")
print(f"🚀 Starting background task processor...")

celery_app = Celery("electoral", broker=config.REDIS_URL, backend=config.REDIS_URL)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_acks_late=True, worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "discover-hourly": {"task": "app.tasks.discover_documents", "schedule": 3600},
        "dispatch-documents": {"task": "app.tasks.dispatch_pending_documents", "schedule": 15},
        "dispatch-units": {"task": "app.tasks.dispatch_pending_units", "schedule": 10},
        "recover-stale-jobs": {"task": "app.tasks.recover_jobs", "schedule": 60},
    },
)

log.info(f"[TASKS] Celery configured with beat schedule: {list(celery_app.conf.beat_schedule.keys())}")
print(f"   Scheduled tasks: {', '.join(celery_app.conf.beat_schedule.keys())}")


@celery_app.task(name="app.tasks.discover_documents")
def discover_documents():
    log.info(f"[TASKS] discover_documents task started")
    print(f"🔍 Scanning for new PDF files...")
    result = discover()
    log.info(f"[TASKS] discover_documents task completed, found {result} documents")
    print(f"[TASKS] discover_documents task completed, found {result} documents")
    return result


@celery_app.task(name="app.tasks.dispatch_pending_documents")
def dispatch_pending_documents():
    log.info(f"[TASKS] dispatch_pending_documents task started")
    print(f"📋 Queuing documents for processing...")
    result = dispatch_documents()
    log.info(f"[TASKS] dispatch_pending_documents task completed, dispatched {result} documents")
    print(f"   {result} documents queued")
    return result


@celery_app.task(name="app.tasks.dispatch_pending_units")
def dispatch_pending_units():
    log.info(f"[TASKS] dispatch_pending_units task started")
    print(f"📋 Queuing work units for extraction...")
    result = dispatch_units()
    log.info(f"[TASKS] dispatch_pending_units task completed, dispatched {result} units")
    print(f"   {result} work units queued")
    return result


@celery_app.task(name="app.tasks.recover_jobs")
def recover_jobs():
    log.info(f"[TASKS] recover_jobs task started")
    print(f"🔄 Checking for stuck or failed jobs...")
    result = recover()
    log.info(f"[TASKS] recover_jobs task completed")
    print(f"   Job recovery check complete")
    return result


@celery_app.task(name="app.tasks.process_document")
def process_document(document_id: str, session_id: str):
    log.info(f"[TASKS] process_document task started for document_id={document_id}, session_id={session_id}")
    print(f"📄 Starting document processing...")
    result = process_document_job(document_id, session_id)
    log.info(f"[TASKS] process_document task completed for document_id={document_id}")
    print(f"   Document processing initiated")
    return result


@celery_app.task(name="app.tasks.process_unit")
def process_unit(document_id: str, session_id: str, unit_id: str):
    log.info(f"[TASKS] process_unit task started for document_id={document_id}, session_id={session_id}, unit_id={unit_id}")
    print(f"🔍 Starting data extraction...")
    result = process_unit_job(document_id, session_id, unit_id)
    log.info(f"[TASKS] process_unit task completed for unit_id={unit_id}")
    print(f"   Data extraction complete")
    return result
