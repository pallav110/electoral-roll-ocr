#!/usr/bin/env python3
"""Trace selected production OCR cards from render through public JSON."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import cv2
import fitz
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "scripts" / "ocr_pdf_api.py"
DEFAULT_PDF = ROOT / "input" / (
    "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
)

SPEC = importlib.util.spec_from_file_location("ocr_pdf_api", API_PATH)
assert SPEC is not None and SPEC.loader is not None
ocr_pdf_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr_pdf_api)


# These are deliberately representative failures from the completed image audit:
# page 3/card 5 house, page 4/card 7 house, page 4/card 9 relation,
# page 6/card 9 age, page 8/card 18 name, and page 9/card 7 name/house.
DEFAULT_CARDS = ((3, 5), (4, 7), (4, 9), (6, 9), (8, 18), (9, 7))


def json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


def save_image(path: Path, image: Any) -> None:
    if image is None:
        return
    if isinstance(image, bytes):
        path.write_bytes(image)
        return
    array = np.asarray(image)
    if array.ndim == 2:
        cv2.imwrite(str(path), array)
    else:
        cv2.imwrite(str(path), array)


def decode_image(image_bytes: bytes) -> np.ndarray:
    image = cv2.imdecode(
        np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("Could not decode rendered card image")
    return image


def run_tesseract_hindi_experiment(
    image: np.ndarray,
    trace_dir: Path,
) -> dict[str, Any]:
    """Compare Hindi Tesseract languages, PSMs, and image preprocessing."""
    result: dict[str, Any] = {
        "purpose": (
            "Diagnostic only: compare Hindi/English Tesseract recognition "
            "without changing production OCR or arbitration."
        ),
        "image_shape": list(image.shape),
        "language_sets": [],
        "variants": [],
        "runs": [],
    }
    try:
        import pytesseract
        from PIL import Image

        available = set(pytesseract.get_languages(config=""))
        result["available_languages"] = sorted(available)
        # pytesseract may omit script packs from get_languages() even though
        # the installed Tesseract binary can load them by explicit name.
        tesseract_path = shutil.which("tesseract") or "tesseract"
        probe = subprocess.run(
            [tesseract_path, "--list-langs"],
            capture_output=True,
            text=True,
            check=False,
        )
        listed_languages = set(probe.stdout.split())
        listed_languages.update(probe.stderr.split())
        result["tesseract_list_languages"] = sorted(listed_languages)
    except Exception as error:
        result["error"] = f"language discovery failed: {type(error).__name__}: {error}"
        return result

    required = {"hin", "eng"}
    if not required.issubset(available):
        result["error"] = (
            "Required Tesseract languages are missing: "
            + ", ".join(sorted(required - available))
        )
        return result

    language_sets = ["hin+eng"]
    listed_languages = set(result.get("tesseract_list_languages", []))
    devanagari_language = next(
        (
            language for language in listed_languages
            if language.replace("\\", "/").lower() == "script/devanagari"
        ),
        "",
    )
    if devanagari_language:
        language_sets.append(f"hin+eng+{devanagari_language}")
    result["language_sets"] = language_sets
    result["devanagari_language"] = devanagari_language

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cubic_gray = cv2.resize(
        gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    cubic_threshold = cv2.threshold(
        cubic_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    variants: list[tuple[str, np.ndarray]] = [
        ("original_color", image),
        ("grayscale", gray),
        ("cubic_grayscale", cubic_gray),
        ("cubic_threshold", cubic_threshold),
    ]
    result["variants"] = [
        {"name": name, "shape": list(value.shape)}
        for name, value in variants
    ]
    for name, value in variants:
        save_image(trace_dir / f"tesseract_hindi_{name}.png", value)

    for language in language_sets:
        for variant_name, variant in variants:
            if variant.ndim == 2:
                pil_image = Image.fromarray(variant)
            else:
                pil_image = Image.fromarray(
                    cv2.cvtColor(variant, cv2.COLOR_BGR2RGB))
            for psm in (6, 7):
                config = f"--oem 3 --psm {psm}"
                try:
                    text = pytesseract.image_to_string(
                        pil_image, lang=language, config=config)
                    normalized = " ".join(text.split())
                    result["runs"].append({
                        "language": language,
                        "variant": variant_name,
                        "psm": psm,
                        "config": config,
                        "text": normalized,
                        "devanagari_chars": len(
                            re.findall(r"[\\u0900-\\u097F]", normalized)),
                        "digits": len(re.findall(r"\\d", normalized)),
                        "slash_count": normalized.count("/"),
                    })
                except Exception as error:
                    result["runs"].append({
                        "language": language,
                        "variant": variant_name,
                        "psm": psm,
                        "config": config,
                        "error": f"{type(error).__name__}: {error}",
                    })
    return result


def card_field_crops(image: np.ndarray) -> dict[str, np.ndarray]:
    height, width = image.shape[:2]

    def crop(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
        return image[
            int(height * y0):int(height * y1),
            int(width * x0):int(width * x1),
        ]

    # Match the production field bands and upscale diagnostic crops so the
    # printed label/value boundaries are visible during review.
    result = {
        "serial": crop(0.03, 0.04, 0.38, 0.22),
        "epic": crop(0.55, 0.04, 0.99, 0.22),
        "name_relation": crop(0.03, 0.22, 0.73, 0.47),
        "house": crop(0.03, 0.45, 0.73, 0.59),
        "age": crop(0.03, 0.59, 0.55, 0.78),
        "photo": crop(0.75, 0.23, 0.99, 0.96),
    }
    return {
        name: cv2.resize(value, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        for name, value in result.items()
    }


def patch_trace_functions(trace: dict[str, Any], trace_dir: Path) -> list[tuple[str, Any]]:
    """Wrap production functions without changing their behavior."""
    originals: list[tuple[str, Any]] = []
    module = ocr_pdf_api

    def wrap(name: str, factory: Callable[[Callable[..., Any]], Callable[..., Any]]) -> None:
        original = getattr(module, name)
        originals.append((name, original))
        setattr(module, name, factory(original))

    def simple(name: str, input_keys: tuple[str, ...] = ()) -> None:
        def factory(original: Callable[..., Any]) -> Callable[..., Any]:
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                result = original(*args, **kwargs)
                entry: dict[str, Any] = {"function": name}
                if name == "parse_voter_box_from_ocr_lines":
                    entry["lines"] = json_safe(args[0] if args else kwargs.get("lines"))
                elif name == "_extract_text_with_tesseract":
                    entry["result"] = json_safe(result)
                elif name == "_extract_relation_fallback_with_tesseract":
                    entry["result"] = json_safe(result)
                else:
                    entry["result"] = json_safe(result)
                trace.setdefault("function_calls", []).append(entry)
                return result
            return wrapped
        wrap(name, factory)

    simple("_extract_text_with_tesseract")
    simple("_extract_relation_fallback_with_tesseract")
    simple("parse_voter_box_from_ocr_lines")
    simple("_extract_tesseract_house_candidates")
    simple("_extract_tesseract_house_prefix")
    simple("_extract_tesseract_house_address")
    simple("_extract_tesseract_age")
    simple("_extract_tesseract_gender")

    def merge_factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            before = json_safe(args[0]) if args else {}
            result = original(*args, **kwargs)
            after = json_safe(args[0]) if args else {}
            trace.setdefault("function_calls", []).append({
                "function": "_merge_focused_name_and_relation",
                "before": before,
                "result": json_safe(result),
                "after": after,
            })
            return result
        return wrapped
    wrap("_merge_focused_name_and_relation", merge_factory)

    def choose_house_factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            trace.setdefault("function_calls", []).append({
                "function": "_choose_house_number",
                "inputs": json_safe({
                    "tesseract_house": args[0] if args else kwargs.get("tesseract_house"),
                    "metadata": args[1] if len(args) > 1 else kwargs.get("metadata"),
                }),
                "result": json_safe(result),
            })
            return result
        return wrapped
    wrap("_choose_house_number", choose_house_factory)

    def choose_age_factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            trace.setdefault("function_calls", []).append({
                "function": "_choose_age",
                "inputs": json_safe({
                    "tesseract_age": args[0] if args else kwargs.get("tesseract_age"),
                    "raw_candidates": args[1] if len(args) > 1 else kwargs.get("raw_candidates"),
                    "focused_age": args[2] if len(args) > 2 else kwargs.get("focused_age", ""),
                }),
                "result": json_safe(result),
            })
            return result
        return wrapped
    wrap("_choose_age", choose_age_factory)

    def paddle_boxes_factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(image: Any, *args: Any, **kwargs: Any) -> Any:
            result = original(image, *args, **kwargs)
            call_index = len(trace.setdefault("paddle_calls", [])) + 1
            array = np.asarray(image)
            if array.ndim == 2:
                overlay = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
            else:
                overlay = array.copy()
            hits: list[dict[str, Any]] = []
            for box, text, confidence in result or []:
                points = np.asarray(box, dtype=np.int32).reshape(-1, 2)
                if len(points) >= 2:
                    cv2.polylines(overlay, [points], True, (0, 0, 255), 2)
                    x, y = points[0].tolist()
                    cv2.putText(
                        overlay, str(text), (int(x), max(16, int(y) - 3)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 0), 1,
                        cv2.LINE_AA,
                    )
                hits.append({
                    "box": json_safe(box),
                    "text": text,
                    "confidence": float(confidence),
                })
            save_image(trace_dir / f"paddle_call_{call_index:03d}.png", overlay)
            trace["paddle_calls"].append({
                "call_index": call_index,
                "image_shape": list(array.shape),
                "hits": hits,
            })
            return result
        return wrapped
    wrap("_paddle_text_with_boxes", paddle_boxes_factory)

    def paddle_metadata_factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            trace.setdefault("function_calls", []).append({
                "function": "_extract_paddle_card_metadata",
                "result": json_safe(result),
            })
            return result
        return wrapped
    wrap("_extract_paddle_card_metadata", paddle_metadata_factory)

    return originals


def trace_card(document: fitz.Document, page_number: int, card_index: int, output: Path) -> dict[str, Any]:
    page = document[page_number - 1]
    card_rect = ocr_pdf_api._voter_card_rects(page)[card_index - 1]
    trace_dir = output / f"page_{page_number:03d}_card_{card_index:02d}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace: dict[str, Any] = {
        "page_number": page_number,
        "card_index": card_index,
        "card_rect": json_safe({
            "x0": card_rect.x0, "y0": card_rect.y0,
            "x1": card_rect.x1, "y1": card_rect.y1,
        }),
        "function_calls": [],
        "paddle_calls": [],
    }

    raw_200 = page.get_pixmap(dpi=200, clip=card_rect, alpha=False).tobytes("png")
    raw_300 = page.get_pixmap(dpi=300, clip=card_rect, alpha=False).tobytes("png")
    cleaned_200 = ocr_pdf_api._clean_card_image_bytes(raw_200)
    cleaned_300 = ocr_pdf_api._clean_card_image_bytes(raw_300)
    original_image = decode_image(raw_300)
    cleaned_image = decode_image(cleaned_300)
    changed = np.any(original_image != cleaned_image, axis=2)
    trace["render"] = {
        "raw_200_bytes": len(raw_200),
        "raw_300_bytes": len(raw_300),
        "cleaned_200_bytes": len(cleaned_200),
        "cleaned_300_bytes": len(cleaned_300),
        "raw_300_shape": list(original_image.shape),
        "cleaned_pixels_changed": int(np.count_nonzero(changed)),
        "cleaned_pixels_changed_fraction": float(np.mean(changed)),
    }
    save_image(trace_dir / "original_200dpi.png", raw_200)
    save_image(trace_dir / "original_300dpi.png", raw_300)
    save_image(trace_dir / "cleaned_200dpi.png", cleaned_200)
    save_image(trace_dir / "cleaned_300dpi.png", cleaned_300)
    field_crops = card_field_crops(cleaned_image)
    for name, crop in field_crops.items():
        save_image(trace_dir / f"crop_{name}.png", crop)

    # Test-only matrix: do not feed any of these results into production.
    trace["tesseract_hindi_experiment"] = run_tesseract_hindi_experiment(
        field_crops["house"], trace_dir)

    originals = patch_trace_functions(trace, trace_dir)
    try:
        internal = ocr_pdf_api._extract_card(
            page,
            card_rect,
            page_number=page_number,
            card_index=card_index,
            rendered_images=(raw_200, raw_300),
        )
    finally:
        for name, original in reversed(originals):
            setattr(ocr_pdf_api, name, original)

    trace["internal_record"] = json_safe(internal)

    if internal is not None:
        trace["public_record"] = json_safe(
            ocr_pdf_api._public_record(internal, 1, "trace.pdf", {})
        )
    (trace_dir / "trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return trace


def parse_cards(value: str | None) -> list[tuple[int, int]]:
    if not value:
        return list(DEFAULT_CARDS)
    result: list[tuple[int, int]] = []
    for item in value.split(","):
        page, card = item.strip().split(":", 1)
        result.append((int(page), int(card)))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "card_pipeline_traces")
    parser.add_argument(
        "--cards",
        help="Comma-separated page:card pairs, e.g. 3:5,4:7",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cards = parse_cards(args.cards)
    summary: list[dict[str, Any]] = []
    with fitz.open(args.pdf) as document:
        for page_number, card_index in cards:
            trace = trace_card(document, page_number, card_index, args.output)
            public = trace.get("public_record") or {}
            summary.append({
                "page": page_number,
                "card": card_index,
                "serial": public.get("voter_sr_no"),
                "house": public.get("house_no"),
                "relation": public.get("relation_name"),
                "age": public.get("age"),
                "name": " ".join(
                    value for value in (
                        public.get("voter_first_name", ""),
                        public.get("voter_middle_name", ""),
                        public.get("voter_sur_name", ""),
                    ) if value
                ),
                "trace_dir": str(
                    args.output / f"page_{page_number:03d}_card_{card_index:02d}"
                ),
            })
    summary_path = args.output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"cards": len(summary), "summary": str(summary_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
