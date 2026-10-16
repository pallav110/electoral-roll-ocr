import logging
import sys

from celery import Celery
from kombu import Queue
from sqlalchemy import exists, select

from app import config
from app.workflow import discover, dispatch_documents, dispatch_units, process_document_job, process_unit_job, recover
from app.archive_ingest import ingest_minio_archives
from app.cover import read_cover, sweep_missing_covers
from app.db import SessionLocal
from app.models import Document, DocumentCover, DocumentLocation, DocumentVisualExtraction
from app.document_locations import geocoder_ready, resolve_document_location
from app.map_verification import verify_next_map
from app.visual_assets import extract_visual_assets


log = logging.getLogger(__name__)

# Three queues, because one queue for everything is what made a 27-page roll take
# eight hours. Everything shared the default "celery" FIFO served by a single
# --concurrency=1 worker, so the beat's own dispatch and recovery messages -- which
# fire every 10 and 60 seconds -- queued up in front of the OCR work they exist to
# schedule. Splitting them means a slow or wedged unit can no longer delay the
# 15-second dispatch tick that refills the window.
#
# Unrouted tasks land on CONTROL, not on the default queue. That default is the
# part that matters: it means adding a task in future cannot silently put it back
# in front of the OCR work.
QUEUE_CONTROL = "control"
QUEUE_INGEST = "ingest"
QUEUE_OCR = "ocr"
# Cover-page reads. Each is a 5-10 s OCR call, so they get their own queue and
# worker: on control they would stall dispatch, on ocr they would wait behind
# multi-hour card extractions and the printed totals would arrive last.
QUEUE_COVER = "cover"
# Visual galleries are deliberately isolated: neither their bulk backfill nor
# a slow PDF render can occupy control, ingest, cover, or voter-OCR workers.
QUEUE_IMAGES = "images"
# Cover-metadata location lookup is isolated from both image rendering and
# voter OCR. The local OSM importer can be unavailable during its first load;
# that must not delay any other pipeline work.
QUEUE_LOCATIONS = "locations"
# Map-image verification is slower than a cover lookup and must not take a
# slot from voter OCR or from gallery rendering. One worker consumes it.
QUEUE_MAP_VERIFY = "map-verify"

log.info(f"[TASKS] Initializing Celery with broker={config.REDIS_URL}")
print(f"🚀 Starting background task processor...")

celery_app = Celery("electoral", broker=config.REDIS_URL, backend=config.REDIS_URL)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_acks_late=True, worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # Each queue gets its own exchange, and its own routing key. Declaring
    # Queue("ocr") on its own is not enough: the exchange defaults to
    # task_default_exchange and the routing key to "" -- the exchange's own
    # name. Both queues then bind to control/control, and a direct exchange
    # delivers a message to every queue bound to the key it was published with.
    # That fans every control message out to the OCR worker as well, which is
    # the queue pile-up this whole change exists to remove. Named explicitly so
    # the binding is ocr/ocr, and only the OCR worker is listening there.
    task_queues=(
        Queue(QUEUE_CONTROL, routing_key=QUEUE_CONTROL),
        Queue(QUEUE_INGEST, routing_key=QUEUE_INGEST),
        Queue(QUEUE_OCR, routing_key=QUEUE_OCR),
        Queue(QUEUE_COVER, routing_key=QUEUE_COVER),
        Queue(QUEUE_IMAGES, routing_key=QUEUE_IMAGES),
        Queue(QUEUE_LOCATIONS, routing_key=QUEUE_LOCATIONS),
        Queue(QUEUE_MAP_VERIFY, routing_key=QUEUE_MAP_VERIFY),
    ),
    task_default_queue=QUEUE_CONTROL,
    task_default_exchange=QUEUE_CONTROL,
    task_routes={
        "app.tasks.process_unit": {"queue": QUEUE_OCR},
        "app.tasks.ingest_minio_zip_archives": {"queue": QUEUE_INGEST},
        # process_document stays on control deliberately. It only counts pages
        # and creates unit rows, so it is fast; routing it to the OCR queue would
        # put document setup behind the very work it is setting up.
        "app.tasks.process_document": {"queue": QUEUE_CONTROL},
        "app.tasks.read_document_cover": {"queue": QUEUE_COVER},
        # The sweep lives on the cover worker too, so the cover feature is
        # self-contained: only worker-cover has to know these tasks exist.
        "app.tasks.sweep_covers": {"queue": QUEUE_COVER},
        "app.tasks.extract_document_images": {"queue": QUEUE_IMAGES},
        "app.tasks.dispatch_visual_images": {"queue": QUEUE_CONTROL},
        "app.tasks.resolve_document_location": {"queue": QUEUE_LOCATIONS},
        "app.tasks.verify_map_image": {"queue": QUEUE_MAP_VERIFY},
        "app.tasks.dispatch_document_locations": {"queue": QUEUE_CONTROL},
    },
    broker_transport_options={"visibility_timeout": config.CELERY_VISIBILITY_TIMEOUT},
    beat_schedule={
        "discover-hourly": {"task": "app.tasks.discover_documents", "schedule": 3600},
        "ingest-minio-archives": {"task": "app.tasks.ingest_minio_zip_archives", "schedule": 30},
        "dispatch-documents": {"task": "app.tasks.dispatch_pending_documents", "schedule": 15},
        "dispatch-units": {"task": "app.tasks.dispatch_pending_units", "schedule": 10},
        "recover-stale-jobs": {"task": "app.tasks.recover_jobs", "schedule": 60},
        "sweep-missing-covers": {"task": "app.tasks.sweep_covers", "schedule": 60},
        # Claims up to 100 completed PDFs per pass.  Claiming before publish
        # prevents duplicate messages even when Beat is restarted.
        "dispatch-visual-images": {"task": "app.tasks.dispatch_visual_images", "schedule": 15},
        "dispatch-document-locations": {"task": "app.tasks.dispatch_document_locations", "schedule": 30},
    },
)

log.info(f"[TASKS] Celery configured with beat schedule: {list(celery_app.conf.beat_schedule.keys())}")
print(f"   Scheduled tasks: {', '.join(celery_app.conf.beat_schedule.keys())}")


def _command_serves_ingest(argv: list[str] | None = None) -> bool:
    """True when this process was started as the ZIP ingest worker."""
    argv = list(sys.argv if argv is None else argv)
    for index, arg in enumerate(argv):
        if arg == "-Q" and index + 1 < len(argv):
            names = {part.strip() for part in argv[index + 1].split(",")}
            return "ingest" in names
        if arg.startswith("--queues="):
            names = {part.strip() for part in arg.split("=", 1)[1].split(",")}
            return "ingest" in names
    return False


if _command_serves_ingest():
    config.refuse_bad_minio_config("ingest")


@celery_app.task(name="app.tasks.ingest_minio_zip_archives")
def ingest_minio_zip_archives():
    log.info("Checking the MinIO bucket for new ZIP archives")
    return ingest_minio_archives()


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


@celery_app.task(name="app.tasks.sweep_covers")
def sweep_covers():
    """Queue a cover-page read for every PDF that does not have one yet."""
    return sweep_missing_covers(lambda document_id: read_document_cover.delay(document_id))


@celery_app.task(name="app.tasks.read_document_cover")
def read_document_cover(document_id: str):
    log.info(f"[TASKS] read_document_cover started for document_id={document_id}")
    return read_cover(document_id)


@celery_app.task(name="app.tasks.dispatch_visual_images")
def dispatch_visual_images(limit: int = 100):
    """Claim completed PDFs for the non-blocking visual-gallery backfill."""
    claimed: list[str] = []
    with SessionLocal.begin() as db:
        completed_or_active = exists(select(DocumentVisualExtraction.id).where(
            DocumentVisualExtraction.document_id == Document.id,
            DocumentVisualExtraction.source_hash == Document.file_hash,
            # The unique key covers every status. Treating a failed row as
            # absent made this periodic dispatcher try to INSERT the same
            # (document, hash) again forever, producing a control-queue error
            # every 15 seconds. On-demand extraction may retry it in place.
            DocumentVisualExtraction.status.in_(("pending", "processing", "completed", "failed")),
        ))
        documents = list(db.scalars(
            select(Document).where(
                Document.status == "completed",
                Document.file_hash.is_not(None),
                ~completed_or_active,
            ).order_by(Document.processing_completed_at, Document.id).limit(limit)
        ))
        for document in documents:
            db.add(DocumentVisualExtraction(
                document_id=document.id, source_hash=document.file_hash, status="pending",
            ))
            claimed.append(str(document.id))
    for document_id in claimed:
        extract_document_images.apply_async(args=(document_id,), queue=QUEUE_IMAGES)
    return {"claimed": len(claimed)}


@celery_app.task(name="app.tasks.extract_document_images")
def extract_document_images(document_id: str, refresh: bool = False):
    """Generate or safely refresh a visual gallery after voter OCR completed.

    ``refresh`` replaces only cached image-panel metadata/assets.  It never
    invokes the voter extractor or changes a document's processing status.
    """
    with SessionLocal() as db:
        document = db.get(Document, document_id)
        if not document or document.status != "completed":
            return {"status": "skipped", "reason": "document_not_completed"}
        # Fast mode preserves actual crops and persistence, but avoids visual
        # text OCR. It cannot change voter data or compete for the OCR API.
        assets = extract_visual_assets(db, document, detailed_labels=False, force=refresh)
        return {"status": "refreshed" if refresh else "completed", "assets": len(assets)}


@celery_app.task(name="app.tasks.dispatch_document_locations")
def dispatch_document_locations(limit: int | None = None):
    """Queue cover-derived location work without holding up extraction.

    Completed documents are backfilled, while documents still being read are
    picked up as soon as their independent cover read completes.
    """
    # Avoid publishing hundreds of known no-op jobs while the one-time India
    # OSM import is still running. The next 30-second beat tick starts the
    # full backfill automatically as soon as /status is live.
    if not geocoder_ready():
        return {"claimed": 0, "reason": "geocoder_not_ready"}
    limit = limit or config.LOCATION_SWEEP_BATCH
    claimed: list[str] = []
    with SessionLocal.begin() as db:
        rows = list(db.scalars(
            select(Document)
            .join(DocumentCover, DocumentCover.document_id == Document.id)
            .outerjoin(DocumentLocation, DocumentLocation.document_id == Document.id)
            .where(
                Document.status != "cancelled",
                DocumentCover.status == "completed",
                (DocumentLocation.id.is_(None))
                | ((DocumentLocation.status == "pending") & (DocumentLocation.attempt_count < config.LOCATION_MAX_ATTEMPTS)),
            )
            .order_by(Document.processing_completed_at.nullslast(), Document.discovered_at)
            # DocumentLocation is optional in this query. PostgreSQL rejects
            # FOR UPDATE on an outer-joined nullable relation, so lock only
            # the durable document claim rather than every joined table.
            .with_for_update(of=Document, skip_locked=True)
            .limit(limit)
        ))
        for document in rows:
            row = db.scalar(select(DocumentLocation).where(DocumentLocation.document_id == document.id))
            if row is None:
                db.add(DocumentLocation(document_id=document.id, status="processing"))
            else:
                # Claim before publish. Without this, every 30-second beat
                # tick can publish the same pending document again while the
                # previous task is still waiting in Redis.
                row.status = "processing"
            claimed.append(str(document.id))
    for document_id in claimed:
        resolve_document_location_task.apply_async(args=(document_id,), queue=QUEUE_LOCATIONS)
    return {"claimed": len(claimed)}


@celery_app.task(name="app.tasks.resolve_document_location")
def resolve_document_location_task(document_id: str):
    with SessionLocal() as db:
        return resolve_document_location(db, document_id)


@celery_app.task(name="app.tasks.verify_map_image")
def verify_map_image():
    """Verify one pending map image. Idle when none are waiting."""
    with SessionLocal() as db:
        return verify_next_map(db)


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
