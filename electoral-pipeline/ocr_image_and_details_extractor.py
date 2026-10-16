#!/usr/bin/env python3
"""Standalone visual-image extractor for electoral-roll PDFs.

This is deliberately *not* wired into Docker Compose, the OCR router, Celery,
Redis, MinIO, or Postgres. It exports only visual map/photo pages and OCR text
read from those exported images. It does not extract electoral-roll metadata,
voter cards, cover details, or database records.

CLI example (page numbers are 1-based)::

    python ocr_image_and_details_extractor.py roll.pdf --pages 2 --ocr

Optional inspection API (also not part of the production stack)::

    python ocr_image_and_details_extractor.py --serve --port 8084
    curl -F 'pdf_file=@roll.pdf' -F 'page_numbers=2' -F 'ocr_images=true' \
      http://127.0.0.1:8084/image-details/extract

Each run creates a manifest, page images, embedded raster images, and optional
OCR text from those images. It never writes to the application database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz
import cv2
import numpy as np
import pytesseract
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from map_georeference import detect_red_pin


APP_NAME = "electoral-image-and-details-extractor"
DEFAULT_OUTPUT_DIR = Path(os.getenv("IMAGE_DETAILS_OUTPUT_DIR", "/tmp/electoral-image-details"))
DEFAULT_PREVIEW_DPI = int(os.getenv("IMAGE_DETAILS_PREVIEW_DPI", "150"))
MAX_UPLOAD_BYTES = int(os.getenv("IMAGE_DETAILS_MAX_UPLOAD_MB", "100")) * 1024 * 1024

app = FastAPI(title=APP_NAME, version="0.1.0")


def _safe_stem(value: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(value).stem).strip(" .")
    return stem or "document"


def _parse_page_numbers(raw: str | None, page_count: int) -> list[int]:
    """Parse a comma-separated, 1-based list such as ``2,4-6``."""
    if not raw or not raw.strip():
        raise ValueError("Page list is required for explicit page selection")
    chosen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            left, right = (part.strip() for part in token.split("-", 1))
            if not left.isdigit() or not right.isdigit():
                raise ValueError(f"Invalid page range: {token!r}")
            start, end = int(left), int(right)
            if start > end:
                raise ValueError(f"Invalid descending page range: {token!r}")
            chosen.update(range(start, end + 1))
        elif token.isdigit():
            chosen.add(int(token))
        else:
            raise ValueError(f"Invalid page number: {token!r}")
    invalid = sorted(page for page in chosen if page < 1 or page > page_count)
    if invalid:
        raise ValueError(f"Page numbers outside 1-{page_count}: {invalid}")
    return sorted(chosen)


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _colourful_pixel_ratio(page: fitz.Page) -> float:
    """Return the fraction of a low-resolution page that contains real colour.

    Electoral voter-card pages are normally black/white scans.  The
    map/polling-station page contains photographs, map pins and coloured
    section bands, so it has a materially higher fraction of non-grey pixels.
    This is a page-selection signal only; it never changes extracted data.
    """
    pixmap = page.get_pixmap(dpi=72, alpha=False)
    if pixmap.n < 3:
        return 0.0
    pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
    rgb = pixels[:, :, :3].astype(np.int16)
    spread = rgb.max(axis=2) - rgb.min(axis=2)
    not_near_white = rgb.mean(axis=2) < 245
    return float(np.mean((spread >= 24) & not_near_white))


def _find_visual_information_pages(doc: fitz.Document) -> list[int]:
    """Find map/photo information pages without exporting first/last pages.

    Page 1 and the final page are intentionally excluded. A middle page is
    selected only when it has substantial real colour consistent with
    maps/photos rather than a monochrome voter grid.
    """
    selected: list[int] = []
    for page_number in range(2, doc.page_count):  # 1-based: excludes first and last.
        page = doc[page_number - 1]
        if _colourful_pixel_ratio(page) >= 0.02:
            selected.append(page_number)
    return selected


def _runs(active: np.ndarray, minimum: int) -> list[tuple[int, int]]:
    """Return contiguous true ranges at least ``minimum`` pixels long."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(np.append(active, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= minimum:
                runs.append((start, index))
            start = None
    return runs


def _detect_visual_panels(page_image: np.ndarray) -> list[dict[str, tuple[int, int, int, int]]]:
    """Find independently useful map/photo/diagram panels on a visual page.

    The rolls place visual material in one or more horizontal bands, often with
    two columns.  We discover the bands from their broad coloured headings and
    discover columns from actual non-white visual content; no fixed count of
    four, five or six panels is assumed.  If no such layout is present, return
    no panels rather than inventing crops.
    """
    if page_image is None or page_image.size == 0:
        return []
    height, width = page_image.shape[:2]
    hsv = cv2.cvtColor(page_image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(page_image, cv2.COLOR_BGR2GRAY)
    # The tested roll uses broad pale-cyan section headings. Keep the range
    # deliberately broad but require it to span most of a row, which ordinary
    # photos/maps do not.
    cyan = (
        (hsv[:, :, 0] >= 78) & (hsv[:, :, 0] <= 108)
        & (hsv[:, :, 1] >= 25) & (hsv[:, :, 1] <= 180)
        & (hsv[:, :, 2] >= 100) & (hsv[:, :, 2] <= 250)
    )
    header_rows = cyan.mean(axis=1) >= 0.55
    header_bands = [
        band for band in _runs(header_rows, max(12, int(height * 0.012)))
        if band[1] - band[0] <= int(height * 0.12)
    ]
    if not header_bands:
        return []

    nonwhite = (gray < 235) | ((hsv[:, :, 1] > 25) & (hsv[:, :, 2] < 245))
    panels: list[dict[str, tuple[int, int, int, int]]] = []
    for index, (header_start, header_end) in enumerate(header_bands):
        y0 = min(height, header_end + max(6, int(height * 0.004)))
        y1 = (
            max(y0, header_bands[index + 1][0] - max(6, int(height * 0.004)))
            if index + 1 < len(header_bands)
            else min(height - max(20, int(height * 0.04)), y0 + int(height * 0.27))
        )
        if y1 - y0 < int(height * 0.06):
            continue
        density = nonwhite[y0:y1].mean(axis=0)
        active_columns = density >= 0.012
        # Join letters/lines belonging to the same visual panel, but preserve
        # the broad whitespace gutter between neighbouring panels.
        join = max(11, int(width * 0.02))
        active_columns = np.convolve(active_columns.astype(np.uint8), np.ones(join, dtype=np.uint8), "same") > 0
        for x0, x1 in _runs(active_columns, max(60, int(width * 0.10))):
            x0 = max(0, x0 - 4)
            x1 = min(width, x1 + 4)
            if (x1 - x0) * (y1 - y0) >= width * height * 0.008:
                panels.append({
                    "bounds": (x0, y0, x1, y1),
                    "heading_bounds": (x0, header_start, x1, header_end),
                })
    return panels


def _panel_identity(heading: str) -> tuple[str, str]:
    """Translate only a visible panel heading into a stable asset category."""
    text = heading.lower()
    if "google" in text and "map" in text:
        return "google_location_map", "Google location map"
    if "key" in text and "map" in text:
        return "key_location_map", "Key location map"
    if "nazri" in text or "नजरी" in heading or "नक्श" in heading:
        return "nazri_map", "Nazri map"
    if "cad" in text:
        return "cad_layout", "CAD layout drawing"
    if "polling" in text or "मतदान" in heading:
        return "polling_station_photo", "Polling-station building photograph"
    return "visual_image", "Visual image"


def _coordinates_from_text(value: str) -> dict[str, float] | None:
    """Return coordinates only when explicitly printed with a coordinate label."""
    if not value:
        return None
    latitude = re.search(r"(?:latitude|lat\.?|अक्षांश)\s*[:=]?\s*(-?\d{1,2}\.\d{4,})", value, re.I)
    longitude = re.search(r"(?:longitude|long\.?|lon\.?|देशांतर)\s*[:=]?\s*(-?\d{1,3}\.\d{4,})", value, re.I)
    if not latitude or not longitude:
        return None
    lat, lon = float(latitude.group(1)), float(longitude.group(1))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return {"latitude": lat, "longitude": lon}
    return None


def map_image_evidence(image_bytes: bytes) -> dict[str, Any]:
    """Read *measurable* evidence from one map image, without geocoding it.

    A screenshot has no embedded GPS location.  This function therefore
    returns only the red-pin pixel and visible English label positions.  The
    separate verifier must later pair labels to independently confirmed OSM
    places and satisfy its geometry/error checks before coordinates exist.
    """
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Image could not be decoded")
    data = pytesseract.image_to_data(
        image, lang="eng", config="--oem 3 --psm 11", output_type=pytesseract.Output.DICT,
    )
    grouped: dict[tuple[int, int, int], list[tuple[str, int, int, int, int]]] = {}
    for index, raw in enumerate(data["text"]):
        word = _one_line(raw)
        if not word or not re.search(r"[A-Za-z]", word):
            continue
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1.0
        if confidence < 25:
            continue
        key = (int(data["block_num"][index]), int(data["par_num"][index]), int(data["line_num"][index]))
        grouped.setdefault(key, []).append((word, int(data["left"][index]), int(data["top"][index]), int(data["width"][index]), int(data["height"][index])))
    labels: list[dict[str, Any]] = []
    for words in grouped.values():
        text = _one_line(" ".join(word[0] for word in words))
        if len(text) < 4:
            continue
        x0, y0 = min(word[1] for word in words), min(word[2] for word in words)
        x1 = max(word[1] + word[3] for word in words)
        y1 = max(word[2] + word[4] for word in words)
        labels.append({
            "text": text,
            "pixel_x": round((x0 + x1) / 2, 1), "pixel_y": round((y0 + y1) / 2, 1),
            "bounds": {"left": x0, "top": y0, "right": x1, "bottom": y1},
        })
    labels.sort(key=lambda item: (item["pixel_y"], item["pixel_x"]))
    return {
        "pin_pixel": detect_red_pin(image),
        "image_width": int(image.shape[1]), "image_height": int(image.shape[0]),
        "labels": labels,
    }


def _write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def extract_pdf_images(
    pdf_bytes: bytes,
    filename: str,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    page_numbers: str | None = None,
    include_visual_ocr: bool = False,
    include_heading_ocr: bool = True,
    ocr_language: str = "hin+eng",
    preview_dpi: int = DEFAULT_PREVIEW_DPI,
) -> dict[str, Any]:
    """Extract visual page images, embedded images, and optional image OCR.

    The function is intentionally pure with respect to the electoral system:
    its only side effect is a new local output directory under ``output_dir``.
    """
    if not pdf_bytes:
        raise ValueError("PDF is empty")
    document_hash = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # PyMuPDF error classes vary by version.
        raise ValueError(f"Cannot open PDF: {exc}") from exc
    try:
        explicit_selection = bool(page_numbers and page_numbers.strip())
        selected_pages = (
            _parse_page_numbers(page_numbers, doc.page_count)
            if explicit_selection
            else _find_visual_information_pages(doc)
        )
        run_dir = output_dir / f"{_safe_stem(filename)}-{document_hash[:12]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        pages_dir = run_dir / "pages"
        assets_dir = run_dir / "embedded-images"
        panels_dir = run_dir / "panels"
        pages_dir.mkdir(exist_ok=True)
        assets_dir.mkdir(exist_ok=True)
        panels_dir.mkdir(exist_ok=True)
        manifest_pages: list[dict[str, Any]] = []
        exported_xrefs: dict[int, str] = {}

        for page_number in selected_pages:
            page = doc[page_number - 1]
            preview_path = pages_dir / f"page-{page_number:03d}-preview.png"
            pixmap = page.get_pixmap(dpi=preview_dpi, alpha=False)
            pixmap.save(str(preview_path))
            page_image = cv2.imread(str(preview_path), cv2.IMREAD_COLOR)
            panel_items: list[dict[str, Any]] = []
            for panel_number, panel in enumerate(_detect_visual_panels(page_image), start=1):
                x0, y0, x1, y1 = panel["bounds"]
                hx0, hy0, hx1, hy1 = panel["heading_bounds"]
                # Bulk gallery generation deliberately skips this OCR pass.
                # It is purely descriptive -- panel detection itself is image
                # based -- and avoiding up to six Tesseract launches per roll
                # lets a demo gallery be prepared without competing with voter
                # OCR.  The on-demand route retains descriptive heading OCR.
                if include_heading_ocr:
                    heading_image = page_image[hy0:hy1, hx0:hx1]
                    heading = _one_line(pytesseract.image_to_string(heading_image, lang="hin+eng", config="--oem 3 --psm 6"))
                    category, description = _panel_identity(heading)
                else:
                    heading = ""
                    category, description = "visual_panel", f"Visual image {panel_number}"
                asset_name = f"{_safe_stem(filename)}_image_{panel_number:02d}_{category}.png"
                asset_path = panels_dir / asset_name
                cv2.imwrite(str(asset_path), page_image[y0:y1, x0:x1])
                panel_item: dict[str, Any] = {
                    "panel_number": panel_number,
                    "asset": str(Path("panels") / asset_name),
                    "heading": heading or None,
                    "category": category,
                    "description": description,
                    "pixel_bounds": {"left": x0, "top": y0, "right": x1, "bottom": y1},
                }
                if include_visual_ocr:
                    panel_item["image_text"] = _one_line(
                        pytesseract.image_to_string(str(asset_path), lang=ocr_language, config="--oem 3 --psm 6")
                    )
                    panel_item["coordinates"] = _coordinates_from_text(panel_item["image_text"])
                panel_items.append(panel_item)

            page_images: list[dict[str, Any]] = []
            for ordinal, image_ref in enumerate(page.get_images(full=True), start=1):
                xref = image_ref[0]
                if xref not in exported_xrefs:
                    extracted = doc.extract_image(xref)
                    extension = extracted.get("ext") or "bin"
                    content = extracted["image"]
                    digest = hashlib.sha256(content).hexdigest()[:12]
                    asset_name = f"xref-{xref}-{digest}.{extension}"
                    _write_bytes(assets_dir / asset_name, content)
                    exported_xrefs[xref] = str(Path("embedded-images") / asset_name)
                image_item: dict[str, Any] = {
                    "ordinal": ordinal,
                    "xref": xref,
                    "width": image_ref[2],
                    "height": image_ref[3],
                    "bits_per_component": image_ref[4],
                    "colorspace": image_ref[5],
                    "asset": exported_xrefs[xref],
                }
                if include_visual_ocr:
                    image_item["image_text"] = _one_line(
                        pytesseract.image_to_string(
                            str(run_dir / exported_xrefs[xref]), lang=ocr_language, config="--oem 3 --psm 6"
                        )
                    )
                page_images.append(image_item)

            page_image_text = ""
            if include_visual_ocr:
                # The preview is an image too. This captures labels positioned
                # around photos/maps even when they are not part of an embedded
                # image object in the PDF.
                page_image_text = _one_line(
                    pytesseract.image_to_string(str(preview_path), lang=ocr_language, config="--oem 3 --psm 6")
                )
            colour_ratio = _colourful_pixel_ratio(page)
            page_item: dict[str, Any] = {
                "page_number": page_number,
                "colourful_pixel_ratio": round(colour_ratio, 5),
                "page_image": str(Path("pages") / preview_path.name),
                "image_panels": panel_items,
                "embedded_images": page_images,
            }
            if include_visual_ocr:
                page_item["page_image_text"] = page_image_text
            manifest_pages.append(page_item)

        manifest = {
            "extractor": APP_NAME,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "document": {
                "filename": filename,
                "sha256": document_hash,
                "page_count": doc.page_count,
                "selected_pages": selected_pages,
                "selection_mode": "explicit_pages" if explicit_selection else "visual_pages_only",
            },
            "output_directory": str(run_dir),
            "pages": manifest_pages,
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return manifest
    finally:
        doc.close()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": APP_NAME, "integrated": False}


@app.post("/image-details/extract")
async def image_details_extract(
    pdf_file: UploadFile = File(...),
    page_numbers: str | None = Form(None, description="1-based pages, for example 2 or 2,4-6. Omit to detect visual map/photo pages only; first and last pages are excluded."),
    ocr_images: bool = Form(False),
    read_headings: bool = Form(True),
    ocr_language: str = Form("hin+eng"),
) -> dict[str, Any]:
    """Inspect an uploaded PDF without interacting with pipeline data stores."""
    raw = await pdf_file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(400, "PDF is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "PDF exceeds IMAGE_DETAILS_MAX_UPLOAD_MB")
    try:
        return extract_pdf_images(
            raw,
            pdf_file.filename or "document.pdf",
            page_numbers=page_numbers,
            include_visual_ocr=ocr_images,
            include_heading_ocr=read_headings,
            ocr_language=ocr_language,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/image-details/georeference")
async def image_georeference(request: Request) -> dict[str, Any]:
    """Fit a pin from caller-supplied controls. Never invents a landmark.

    The running server loads this module at process start. A separate
    map-verify helper imports the same function from disk so a reload of
    this process is not required to enforce the fit.
    """
    from map_georeference import ControlPoint, georeference_pin

    payload = await request.json()
    pin = payload.get("pin")
    controls = [
        ControlPoint(
            label=str(item["label"]),
            pixel_x=float(item["pixel_x"]),
            pixel_y=float(item["pixel_y"]),
            latitude=float(item["latitude"]),
            longitude=float(item["longitude"]),
        )
        for item in payload.get("controls") or []
    ]
    return georeference_pin(tuple(pin) if pin else None, controls)


@app.post("/image-details/map-evidence")
async def image_map_evidence(image_file: UploadFile = File(...)) -> dict[str, Any]:
    """Return map-label and red-pin evidence; never returns a guessed GPS pin."""
    raw = await image_file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(400, "Image is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image exceeds IMAGE_DETAILS_MAX_UPLOAD_MB")
    try:
        return map_image_evidence(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/image-details/assets/{run_id}/{asset_path:path}")
def image_asset(run_id: str, asset_path: str) -> FileResponse:
    """Return one generated asset; callers should expose this only via auth."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise HTTPException(404)
    base = (DEFAULT_OUTPUT_DIR / run_id).resolve()
    target = (base / asset_path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF to inspect")
    parser.add_argument("--pages", help="1-based pages, for example 2 or 2,4-6. Omit to export detected visual map/photo pages only (never first or last page).")
    parser.add_argument("--ocr", action="store_true", help="Run visual Tesseract OCR on selected previews")
    parser.add_argument("--ocr-language", default="hin+eng")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--preview-dpi", type=int, default=DEFAULT_PREVIEW_DPI)
    parser.add_argument("--serve", action="store_true", help="Start the optional isolated FastAPI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8084)
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.pdf is None:
        parser.error("pdf is required unless --serve is used")
    if not args.pdf.is_file():
        parser.error(f"PDF not found: {args.pdf}")
    try:
        result = extract_pdf_images(
            args.pdf.read_bytes(), args.pdf.name, output_dir=args.output_dir,
            page_numbers=args.pages, include_visual_ocr=args.ocr,
            ocr_language=args.ocr_language, preview_dpi=args.preview_dpi,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
