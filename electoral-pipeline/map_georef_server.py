"""Local map-evidence and georeference helper.

Runs beside the image extractor, on a different port, so the extractor
process that is already rendering PDF panels does not have to reload.
It reads labels and the red pin. It does not geocode and it does not
choose a GPS coordinate on its own.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytesseract

from map_georeference import ControlPoint, detect_red_pin, georeference_pin


def _ocr_lines(image: np.ndarray, lang: str) -> list[dict]:
    data = pytesseract.image_to_data(image, lang=lang, config="--psm 11", output_type=pytesseract.Output.DICT)
    grouped: dict[tuple, list[int]] = {}
    for index, text in enumerate(data["text"]):
        token = (text or "").strip()
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1
        if len(token) < 2 or confidence < 35:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        grouped.setdefault(key, []).append(index)
    lines = []
    for indexes in grouped.values():
        words = [data["text"][index].strip() for index in indexes]
        left = min(data["left"][index] for index in indexes)
        top = min(data["top"][index] for index in indexes)
        right = max(data["left"][index] + data["width"][index] for index in indexes)
        bottom = max(data["top"][index] + data["height"][index] for index in indexes)
        confidence = float(np.mean([float(data["conf"][index]) for index in indexes]))
        lines.append({
            "text": " ".join(word for word in words if word),
            "pixel_x": (left + right) / 2,
            "pixel_y": (top + bottom) / 2,
            "bounds": {"left": int(left), "top": int(top), "right": int(right), "bottom": int(bottom)},
            "confidence": round(confidence, 1),
        })
    return lines


def _variants(image: np.ndarray) -> list[tuple[np.ndarray, float]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sharp = cv2.addWeighted(image, 1.6, cv2.GaussianBlur(image, (0, 0), 2), -0.6, 0)
    _, threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return [
        (image, 1.0),
        (cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2.0),
        (cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 1.0),
        (cv2.cvtColor(threshold, cv2.COLOR_GRAY2BGR), 1.0),
        (sharp, 1.0),
    ]


def _icons(image: np.ndarray) -> list[tuple[float, float]]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (90, 60, 70), (140, 255, 255))
    mask = mask | cv2.inRange(hsv, (8, 70, 70), (28, 255, 255))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    icons = []
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if 12 <= area <= 500 and 4 <= width <= 40 and 4 <= height <= 40:
            icons.append((float(centroids[index][0]), float(centroids[index][1])))
    return icons


def _nearest_icon(label: dict, icons: list[tuple[float, float]]) -> tuple[float, float] | None:
    best = None
    best_distance = 70.0
    for icon_x, icon_y in icons:
        # Labels sit above or beside the marker, not on the far side of the map.
        if icon_y + 15 < label["bounds"]["top"]:
            continue
        distance = ((icon_x - label["pixel_x"]) ** 2 + (icon_y - label["pixel_y"]) ** 2) ** 0.5
        if distance < best_distance:
            best, best_distance = (icon_x, icon_y), distance
    return best


def rich_evidence(image_bytes: bytes) -> dict:
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return {"pin_pixel": None, "labels": [], "image_width": 0, "image_height": 0}
    height, width = image.shape[:2]
    pin = detect_red_pin(image)
    merged: dict[str, dict] = {}
    for variant, scale in _variants(image):
        for lang in ("eng", "hin+eng"):
            try:
                lines = _ocr_lines(variant, lang)
            except Exception:
                continue
            for line in lines:
                line["pixel_x"] = round(line["pixel_x"] / scale, 1)
                line["pixel_y"] = round(line["pixel_y"] / scale, 1)
                for key, value in line["bounds"].items():
                    line["bounds"][key] = int(round(value / scale))
                key = "".join(ch for ch in line["text"].casefold() if ch.isalnum())
                current = merged.get(key)
                if current is None or line["confidence"] > current["confidence"]:
                    merged[key] = line
    icons = _icons(image)
    labels = []
    for line in merged.values():
        icon = _nearest_icon(line, icons)
        line["icon_pixel"] = [round(icon[0], 1), round(icon[1], 1)] if icon else None
        labels.append(line)
    return {
        "pin_pixel": [round(pin[0], 1), round(pin[1], 1)] if pin else None,
        "image_width": width,
        "image_height": height,
        "labels": labels,
    }


def _georeference(payload: dict) -> dict:
    pin = payload.get("pin")
    controls = [
        ControlPoint(
            label=str(item["label"]), pixel_x=float(item["pixel_x"]), pixel_y=float(item["pixel_y"]),
            latitude=float(item["latitude"]), longitude=float(item["longitude"]),
        )
        for item in payload.get("controls") or []
    ]
    return georeference_pin(tuple(pin) if pin else None, controls)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path == "/georeference":
            self._json(200, _georeference(json.loads(raw.decode())))
            return
        if self.path == "/map-evidence-rich":
            self._json(200, rich_evidence(raw))
            return
        self._json(404, {"error": "not_found"})


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8095), Handler).serve_forever()


if __name__ == "__main__":
    main()
