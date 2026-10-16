"""On-demand visual-page extraction, cached in MinIO and indexed in Postgres."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os

import httpx
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.archive_ingest import s3_client
from app.document_store import open_pdf
from app.models import Document, DocumentVisualAsset, DocumentVisualExtraction

SERVICE_URL = os.getenv("IMAGE_EXTRACTOR_URL", "http://image-extractor:8084")


def _bucket(client) -> None:
    try:
        client.head_bucket(Bucket=config.MINIO_DERIVED_ASSETS_BUCKET)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=config.MINIO_DERIVED_ASSETS_BUCKET)


def assets_for_document(db: Session, document_id) -> list[DocumentVisualAsset]:
    return list(db.scalars(select(DocumentVisualAsset).join(DocumentVisualExtraction).where(
        DocumentVisualExtraction.document_id == document_id,
        DocumentVisualExtraction.status == "completed",
    ).order_by(DocumentVisualAsset.page_number, DocumentVisualAsset.image_number)))


def extract_visual_assets(
    db: Session, document: Document, *, detailed_labels: bool = True,
    force: bool = False,
) -> list[DocumentVisualAsset]:
    source_hash = document.file_hash
    with open_pdf(document.source_path) as stream:
        raw = stream.read()
    source_hash = source_hash or hashlib.sha256(raw).hexdigest()
    existing = db.scalar(select(DocumentVisualExtraction).where(
        DocumentVisualExtraction.document_id == document.id,
        DocumentVisualExtraction.source_hash == source_hash,
    ))
    # ``force`` is intentionally limited to the independent visual gallery.
    # It re-reads panel headings/categories and replaces only its MinIO/DB
    # cache; voter records, OCR units and the source PDF are never touched.
    if existing and existing.status == "completed" and not force:
        return assets_for_document(db, document.id)
    if not existing:
        existing = DocumentVisualExtraction(document_id=document.id, source_hash=source_hash, status="processing")
        db.add(existing); db.flush()
    else:
        existing.status, existing.error = "processing", None
        db.query(DocumentVisualAsset).filter(DocumentVisualAsset.extraction_id == existing.id).delete()
    try:
        response = httpx.post(
            f"{SERVICE_URL}/image-details/extract",
            files={"pdf_file": (document.file_name, raw, "application/pdf")},
        # Keep the gallery fast by skipping OCR *inside* every image, but
        # always read the narrow section heading above each panel.  Those few
        # words are how a Google location map is distinguished from a photo or
        # a generic visual panel; omitting them made the UI say "Visual image".
        data={"ocr_images": str(detailed_labels).lower(), "read_headings": "true"},
            timeout=120,
        )
        response.raise_for_status(); manifest = response.json()
        client = s3_client(); _bucket(client)
        run_id = Path(manifest["output_directory"]).name
        for page in manifest["pages"]:
            for panel in page["image_panels"]:
                artifact = httpx.get(f"{SERVICE_URL}/image-details/assets/{run_id}/{panel['asset']}", timeout=60)
                artifact.raise_for_status()
                filename = Path(panel["asset"]).name
                key = f"derived-images/{source_hash}/page-{page['page_number']:03d}/{filename}"
                client.put_object(Bucket=config.MINIO_DERIVED_ASSETS_BUCKET, Key=key, Body=artifact.content, ContentType="image/png")
                category = panel["category"]
                description = panel["description"]
                heading = panel.get("heading")
                image_text = panel.get("image_text")
                # A blurred/missing heading must not turn a Google map into a
                # meaningless "Visual image" card. Detect only the distinct
                # red map pin here; it classifies the asset but does not infer
                # or store any GPS coordinate.
                if category in {"visual_image", "visual_panel"}:
                    try:
                        evidence = httpx.post(
                            f"{SERVICE_URL}/image-details/map-evidence",
                            files={"image_file": (filename, artifact.content, "image/png")}, timeout=30,
                        )
                        evidence.raise_for_status()
                        result = evidence.json()
                        if result.get("pin_pixel"):
                            category, description = "google_location_map", "Google location map"
                            heading = heading or "Google location map (red pin detected)"
                            labels = [item["text"] for item in result.get("labels", [])]
                            image_text = "\n".join(labels) or image_text
                    except httpx.HTTPError:
                        # Gallery rendering must never fail merely because
                        # optional map classification is unavailable.
                        pass
                coords = panel.get("coordinates") or {}
                db.add(DocumentVisualAsset(extraction_id=existing.id, page_number=page["page_number"], image_number=panel["panel_number"], object_key=key, filename=filename, category=category, description=description, heading=heading, image_text=image_text, latitude=coords.get("latitude"), longitude=coords.get("longitude")))
        existing.status, existing.completed_at = "completed", datetime.now(timezone.utc)
        db.commit()
        return assets_for_document(db, document.id)
    except Exception as exc:
        existing.status, existing.error = "failed", str(exc)
        db.commit(); raise
