"""Verify a Google-map screenshot from landmarks, or leave it unverified.

A coordinate is written only when at least four named landmarks match the
local India map in the right district and state, each label is tied to a
visible marker, and the existing affine fit stays inside the accuracy gates.
A polling-station cover search is never treated as the red pin.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import re
import unicodedata

import httpx
import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.archive_ingest import s3_client
from app.document_store import open_pdf
from app.models import (
    Document, DocumentCover, DocumentMapVerification, DocumentVisualAsset, DocumentVisualExtraction,
)


METHOD = "landmark-affine-v1"
OFFICIAL_PDF_METHOD = "official-pdf-labeled-coordinate-v1"
USER_REASON = (
    "The map image was found, but the visible landmarks could not be matched "
    "to enough authoritative map records to verify the red pin."
)
# A previous one-label search placed a Loni roll on this Mumbai point.
FORBIDDEN_LATITUDE = 19.105098
FORBIDDEN_LONGITUDE = 72.828534
_GENERIC = {
    "mandir", "temple", "masjid", "mosque", "school", "store", "general",
    "road", "gali", "park", "colony", "nagar", "chowk", "market",
    "मंदिर", "मन्दिर", "मस्जिद", "स्कूल", "दुकान", "रोड", "गली", "पार्क",
    "कालोनी", "कॉलोनी", "नगर",
}
_PLACE_TOKENS = {
    "loni", "ghaziabad", "looni", "india", "uttar", "pradesh",
    "लोनी", "गाजियाबाद", "उत्तर", "प्रदेश",
}
_REJECT_CLASSES = {"highway", "boundary", "place"}
_ALIASES = {
    "गाजियाबाद": ("ghaziabad",),
    "लोनी": ("loni",),
    "उत्तरप्रदेश": ("uttarpradesh",),
    "ghaziabad": ("गाजियाबाद",),
    "loni": ("लोनी",),
    "uttarpradesh": ("उत्तरप्रदेश",),
}

# This deliberately accepts only a coordinate next to an explicit printed
# label.  A date, part number, map scale, or arbitrary pair of decimals in a
# roll must never become a polling-station location.
_OFFICIAL_LATITUDE = re.compile(
    r"(?:latitude|lat\.?|अक्षांश)\s*[:=\-]?\s*(-?\d{1,2}\.\d{4,})", re.I
)
_OFFICIAL_LONGITUDE = re.compile(
    r"(?:longitude|long\.?|lon\.?|देशांतर)\s*[:=\-]?\s*(-?\d{1,3}\.\d{4,})", re.I
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def official_coordinates_from_roll_pdf(source_path: str, page_number: int) -> dict | None:
    """Read labelled coordinates printed in the original official PDF page.

    This is intentionally PDF-text only. We do not OCR a blurry map looking
    for numbers: that could turn a date or map label into a confident-looking
    GPS point. Image-only rolls continue through the landmark verifier and
    remain unverified when there are insufficient controls.
    """
    try:
        with open_pdf(source_path) as stream:
            pdf = fitz.open(stream=stream.read(), filetype="pdf")
        try:
            if page_number < 1 or page_number > pdf.page_count:
                return None
            text = pdf[page_number - 1].get_text("text")
        finally:
            pdf.close()
    except (OSError, RuntimeError, ValueError, fitz.FileDataError):
        return None
    latitude = _OFFICIAL_LATITUDE.search(text)
    longitude = _OFFICIAL_LONGITUDE.search(text)
    if not latitude or not longitude:
        return None
    lat, lon = float(latitude.group(1)), float(longitude.group(1))
    # India bounding box is a further guard in addition to the labels.
    if not (6.0 <= lat <= 38.0 and 68.0 <= lon <= 98.0):
        return None
    return {
        "latitude": lat,
        "longitude": lon,
        "page_number": page_number,
        "source": "official_pdf_labeled_coordinate",
    }


def _finish_official_coordinate(row: DocumentMapVerification, coordinates: dict) -> None:
    """Persist a direct official coordinate with explicit provenance."""
    row.status = "verified"
    row.latitude = float(coordinates["latitude"])
    row.longitude = float(coordinates["longitude"])
    row.source_landmarks = [coordinates]
    row.pin_pixel = None
    row.transformation_metrics = {"source": coordinates["source"], "page_number": coordinates["page_number"]}
    row.rmse_meters = None
    row.max_residual_meters = None
    row.method = OFFICIAL_PDF_METHOD
    row.error_reason = None
    row.completed_at = _now()


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip(" ,;:-")


def _fold(value: str | None) -> str:
    # Keep Devanagari marks. A word-character class splits them off the consonant
    # and then a name such as सनातन no longer matches itself.
    return re.sub(r"[^0-9a-z\u0900-\u097f]+", "", _clean(value).casefold())


def _tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z\u0900-\u097F]{3,}", _clean(value))


def specific_labels(labels: list[dict]) -> list[dict]:
    """Keep labels that name something more specific than a generic POI type."""
    kept = []
    for label in labels:
        text = _clean(label.get("text"))
        tokens = [token for token in _tokens(text) if _fold(token) not in _GENERIC and token not in _GENERIC]
        if not tokens:
            continue
        if not label.get("icon_pixel"):
            # The printed name is offset from the marker. Without the marker
            # pixel this label cannot be a control point.
            label = {**label, "control_pixel": None, "rejected": "no_marker_pixel"}
        else:
            label = {**label, "control_pixel": label["icon_pixel"], "rejected": None}
        kept.append(label)
    return kept


def _present(value: str | None, haystack: str) -> bool:
    folded = _fold(value)
    if not folded:
        return False
    if folded in haystack:
        return True
    return any(_fold(alias) in haystack for alias in _ALIASES.get(folded, ()))


def forbidden_point(latitude, longitude) -> bool:
    try:
        return (
            abs(float(latitude) - FORBIDDEN_LATITUDE) < 0.002
            and abs(float(longitude) - FORBIDDEN_LONGITUDE) < 0.002
        )
    except (TypeError, ValueError):
        return False


def acceptable_match(label: str, candidate: dict, cover: DocumentCover | None) -> bool:
    """A locality or road centroid is not a landmark control."""
    if str(candidate.get("class") or "") in _REJECT_CLASSES:
        return False
    if forbidden_point(candidate.get("lat"), candidate.get("lon")):
        return False
    address = candidate.get("address") or {}
    haystack = _fold(" ".join(str(value) for value in (
        candidate.get("display_name"), candidate.get("name"), *address.values(),
    )))
    if "india" not in haystack and str(address.get("country_code", "")).casefold() != "in":
        return False
    district = _clean(cover.district) if cover else ""
    state = _clean(cover.state_name) if cover else ""
    if not _present(district, haystack) or not _present(state, haystack):
        return False
    name = _fold(candidate.get("name") or str(candidate.get("display_name") or "").split(",")[0])
    specific = [
        token for token in _tokens(label)
        if _fold(token) not in _GENERIC and _fold(token) not in _PLACE_TOKENS and token not in _GENERIC
    ]
    return any(_fold(token) in name for token in specific)


def search_context(cover: DocumentCover | None) -> str:
    """Locality, district, state, and India. English aliases are search hints only."""
    if cover is None:
        return "India"
    parts = [
        _clean(cover.village), _clean(cover.tehsil), _clean(cover.district),
        _clean(cover.state_name), "India",
    ]
    aliases = []
    for part in parts:
        aliases.extend(_ALIASES.get(_fold(part), ()))
    seen: set[str] = set()
    return ", ".join(
        part for part in (*parts, *aliases)
        if part and not (part.casefold() in seen or seen.add(part.casefold()))
    )


def coordinates_to_store(status: str, latitude, longitude) -> tuple[float | None, float | None]:
    """Failed and unverified fits never persist a coordinate."""
    if status != "verified" or latitude is None or longitude is None:
        return None, None
    if forbidden_point(latitude, longitude):
        return None, None
    return float(latitude), float(longitude)


def verifications_for_document(db: Session, document_id) -> dict[str, DocumentMapVerification]:
    rows = db.scalars(select(DocumentMapVerification).where(DocumentMapVerification.document_id == document_id))
    return {str(row.visual_asset_id): row for row in rows}


def _evidence(image: bytes) -> dict:
    rich_url = os.getenv("MAP_EVIDENCE_URL", "http://image-extractor:8095/map-evidence-rich")
    try:
        response = httpx.post(rich_url, content=image, timeout=90)
        if response.status_code == 200:
            return response.json()
    except httpx.HTTPError:
        pass
    response = httpx.post(
        f"{os.getenv('IMAGE_EXTRACTOR_URL', 'http://image-extractor:8084').rstrip('/')}/image-details/map-evidence",
        files={"image_file": ("map.png", image, "image/png")},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    for label in payload.get("labels") or []:
        label.setdefault("confidence", None)
        label.setdefault("icon_pixel", None)
    return payload


def _geometry(pin, controls: list[dict]) -> dict | None:
    payload = {"pin": list(pin) if pin else None, "controls": controls}
    urls = [
        os.getenv("MAP_GEOREF_URL", "http://image-extractor:8095/georeference"),
        f"{os.getenv('IMAGE_EXTRACTOR_URL', 'http://image-extractor:8084').rstrip('/')}/image-details/georeference",
    ]
    for url in urls:
        try:
            response = httpx.post(url, json=payload, timeout=30)
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return response.json()
    return None


def _lookup(label: str, context: str) -> list[dict]:
    response = httpx.get(
        f"{config.LOCATION_GEOCODER_URL.rstrip('/')}/search",
        params={
            "q": f"{label}, {context}",
            "format": "jsonv2", "addressdetails": 1, "limit": 3, "countrycodes": "in",
        },
        timeout=config.LOCATION_GEOCODER_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _fingerprint(evidence: dict) -> str:
    labels = sorted(_fold(item.get("text")) for item in evidence.get("labels") or [])
    raw = repr((evidence.get("pin_pixel"), labels))
    return hashlib.sha256(raw.encode()).hexdigest()


def _finish(row: DocumentMapVerification, status: str, reason: str, evidence: dict, landmarks: list, geometry: dict | None) -> None:
    latitude = longitude = None
    if geometry:
        latitude, longitude = coordinates_to_store(geometry.get("status"), geometry.get("latitude"), geometry.get("longitude"))
        if geometry.get("status") == "verified" and (latitude is None or longitude is None):
            status, reason = "unverified", "forbidden_or_unmeasured_coordinate"
    if status != "verified":
        latitude, longitude = None, None
    row.status = status
    row.latitude, row.longitude = latitude, longitude
    row.source_landmarks = landmarks
    row.pin_pixel = {"x": evidence.get("pin_pixel")[0], "y": evidence.get("pin_pixel")[1]} if evidence.get("pin_pixel") else None
    row.transformation_metrics = {
        "fingerprint": _fingerprint(evidence),
        "geometry": {key: geometry.get(key) for key in ("status", "reason", "inlier_control_points", "control_points")} if geometry else None,
        "user_reason": None if status == "verified" else USER_REASON,
    }
    row.rmse_meters = geometry.get("rmse_metres") if geometry and status == "verified" else None
    row.max_residual_meters = geometry.get("max_residual_metres") if geometry and status == "verified" else None
    row.method = METHOD
    row.error_reason = None if status == "verified" else reason
    row.completed_at = _now()


def verify_visual_asset(db: Session, asset_id) -> dict:
    asset = db.get(DocumentVisualAsset, asset_id)
    if asset is None:
        return {"status": "missing"}
    extraction = db.get(DocumentVisualExtraction, asset.extraction_id)
    if extraction is None:
        return {"status": "missing"}
    if asset.category != "google_location_map":
        return {"status": "skipped", "reason": "not_a_google_location_map"}
    row = db.scalar(select(DocumentMapVerification).where(DocumentMapVerification.visual_asset_id == asset.id))
    if row is None:
        row = DocumentMapVerification(document_id=extraction.document_id, visual_asset_id=asset.id, status="processing", method=METHOD)
        db.add(row)
        db.flush()
    elif row.status == "processing":
        return {"status": "processing"}

    # Some CEO/ECI roll formats publish labelled Latitude/Longitude beside the
    # official Google-map panel. That direct official value outranks every OCR
    # and OSM inference path. It is page-scoped: a coordinate on another page
    # cannot silently be copied to this map image.
    document = db.get(Document, extraction.document_id)
    if document:
        official_coordinates = official_coordinates_from_roll_pdf(document.source_path, asset.page_number)
        if official_coordinates:
            _finish_official_coordinate(row, official_coordinates)
            asset.latitude, asset.longitude = None, None
            db.commit()
            return {
                "status": "verified",
                "reason": "official_pdf_labeled_coordinate",
                "latitude": row.latitude,
                "longitude": row.longitude,
            }

    image = s3_client().get_object(Bucket=config.MINIO_DERIVED_ASSETS_BUCKET, Key=asset.object_key)["Body"].read()
    try:
        evidence = _evidence(image)
    except httpx.HTTPError as exc:
        row.status, row.error_reason, row.completed_at = "pending", f"evidence_unavailable: {exc}", None
        db.commit()
        return {"status": "pending"}
    fingerprint = _fingerprint(evidence)
    if row.status in {"verified", "unverified", "failed"} and (row.transformation_metrics or {}).get("fingerprint") == fingerprint:
        return {"status": row.status, "repeated": True}
    cover = db.scalar(select(DocumentCover).where(DocumentCover.document_id == extraction.document_id))
    labels = specific_labels(evidence.get("labels") or [])
    landmarks: list[dict] = []
    controls: list[dict] = []
    context = search_context(cover)
    try:
        for label in labels[:6]:
            text = _clean(label.get("text"))
            record = {"label": text, "confidence": label.get("confidence"), "icon_pixel": label.get("icon_pixel")}
            if label.get("rejected"):
                record["matched"] = False
                record["reason"] = label["rejected"]
                landmarks.append(record)
                continue
            matches = _lookup(text, context)
            chosen = next((item for item in matches if acceptable_match(text, item, cover)), None)
            if chosen is None:
                record["matched"] = False
                record["reason"] = "no_authoritative_local_match"
                landmarks.append(record)
                continue
            record.update({
                "matched": True,
                "source_latitude": float(chosen["lat"]),
                "source_longitude": float(chosen["lon"]),
                "source_name": chosen.get("name") or chosen.get("display_name"),
                "osm_class": chosen.get("class"),
            })
            landmarks.append(record)
            pixel = label["control_pixel"]
            controls.append({
                "label": text, "pixel_x": pixel[0], "pixel_y": pixel[1],
                "latitude": float(chosen["lat"]), "longitude": float(chosen["lon"]),
            })
    except httpx.HTTPError as exc:
        row.status, row.error_reason, row.completed_at = "pending", f"geocoder_unavailable: {exc}", None
        db.commit()
        return {"status": "pending"}
    if not evidence.get("pin_pixel"):
        _finish(row, "unverified", "red_pin_not_detected", evidence, landmarks, None)
    elif len(controls) < 4:
        _finish(row, "unverified", "insufficient_authoritative_landmark_controls", evidence, landmarks, None)
    else:
        geometry = _geometry(evidence.get("pin_pixel"), controls)
        if geometry is None:
            attempts = int((row.transformation_metrics or {}).get("attempts") or 0) + 1
            row.transformation_metrics = {"fingerprint": fingerprint, "attempts": attempts}
            if attempts >= 3:
                _finish(row, "unverified", "geometry_unavailable", evidence, landmarks, None)
            else:
                row.status, row.error_reason, row.completed_at = "pending", "geometry_unavailable", None
                db.commit()
                return {"status": "pending"}
        else:
            _finish(row, geometry.get("status") or "unverified", geometry.get("reason") or "unverified", evidence, landmarks, geometry)
    # The visual-asset coordinate is not an audit trail. Leave it empty so a
    # later reader cannot mistake a cover search for this image's red pin.
    asset.latitude, asset.longitude = None, None
    db.commit()
    return {"status": row.status, "reason": row.error_reason, "latitude": row.latitude, "longitude": row.longitude}


def verify_next_map(db: Session) -> dict:
    asset_id = db.scalar(
        select(DocumentVisualAsset.id)
        .outerjoin(DocumentMapVerification, DocumentMapVerification.visual_asset_id == DocumentVisualAsset.id)
        .where(
            DocumentVisualAsset.category == "google_location_map",
            (DocumentMapVerification.id.is_(None)) | (DocumentMapVerification.status == "pending"),
        )
        .limit(1)
    )
    if asset_id is None:
        return {"status": "idle"}
    return verify_visual_asset(db, asset_id)
