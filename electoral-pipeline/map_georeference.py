"""Conservative georeferencing for polling-station map screenshots.

A Google-map screenshot is not a GPS source: a coordinate is emitted only when
at least four independently matched landmarks let us measure the transform's
error.  A low-quality or under-labelled screenshot therefore returns a clear
``unverified`` result instead of a plausible but wrong pin.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import cos, radians
from typing import Iterable

import cv2
import numpy as np


EARTH_METRES_PER_DEGREE = 111_320.0
MAX_ACCEPTED_RMSE_METRES = 100.0
MAX_ACCEPTED_RESIDUAL_METRES = 150.0


@dataclass(frozen=True)
class ControlPoint:
    """An OCR landmark's image position paired with its verified OSM point."""
    label: str
    pixel_x: float
    pixel_y: float
    latitude: float
    longitude: float


def detect_red_pin(image: np.ndarray) -> tuple[float, float] | None:
    """Return the centre of the strongest red map-marker candidate.

    Red roads and icons are deliberately rejected by minimum area and by
    compactness. For a Google-style teardrop marker, the geographic point is
    its lower tip, rather than the centre of the red shape.  Returning the tip
    avoids a systematic map-scale offset.
    """
    if image is None or image.size == 0:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Red wraps around the HSV hue range, hence two masks.
    mask = cv2.inRange(hsv, (0, 100, 90), (12, 255, 255)) | cv2.inRange(hsv, (168, 100, 90), (180, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    candidates: list[tuple[float, float, float]] = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 18 or width < 4 or height < 6:
            continue
        # A map pin is tall-ish and compact; a road is very elongated.
        aspect = height / max(width, 1)
        fill = area / max(width * height, 1)
        if aspect < 0.75 or aspect > 3.8 or fill < 0.25:
            continue
        score = area * min(aspect, 2.2) * fill
        # The marker tip is the lowest red pixel.  The median x of the bottom
        # two rows is stable even when JPEG/PNG antialiasing removes one pixel
        # of the very point.
        component_y, component_x = np.where(labels == index)
        bottom_y = int(component_y.max())
        bottom_x = component_x[component_y >= bottom_y - 1]
        tip_x = float(np.median(bottom_x))
        candidates.append((score, tip_x, float(bottom_y)))
    if not candidates:
        return None
    _, x, y = max(candidates)
    return x, y


def _project(latitudes: np.ndarray, longitudes: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Small-area WGS84 approximation expressed in metres around the mean."""
    origin_lat = float(np.mean(latitudes))
    origin_lon = float(np.mean(longitudes))
    x = (longitudes - origin_lon) * EARTH_METRES_PER_DEGREE * cos(radians(origin_lat))
    y = (latitudes - origin_lat) * EARTH_METRES_PER_DEGREE
    return x, y, origin_lat, origin_lon


def _unproject(x: float, y: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    lat = origin_lat + y / EARTH_METRES_PER_DEGREE
    lon = origin_lon + x / (EARTH_METRES_PER_DEGREE * cos(radians(origin_lat)))
    return lat, lon


def georeference_pin(pin: tuple[float, float] | None, controls: Iterable[ControlPoint]) -> dict:
    """Estimate GPS for a pin and report measured accuracy, never guess.

    Uses an affine model with RANSAC. Three non-collinear controls are the
    mathematical minimum to solve the equation, but four are required here:
    without an independent fourth point there is no residual error to measure.
    """
    points = list(controls)
    if pin is None:
        return {"status": "unverified", "reason": "red_pin_not_detected", "control_points": len(points)}
    if len(points) < 4:
        return {"status": "unverified", "reason": "fewer_than_four_landmarks", "control_points": len(points), "pin_pixel": pin}
    pixels = np.float32([[point.pixel_x, point.pixel_y] for point in points])
    lats = np.asarray([point.latitude for point in points], dtype=float)
    lons = np.asarray([point.longitude for point in points], dtype=float)
    metres_x, metres_y, origin_lat, origin_lon = _project(lats, lons)
    world = np.float32(np.column_stack((metres_x, metres_y)))
    transform, inliers = cv2.estimateAffine2D(pixels, world, method=cv2.RANSAC, ransacReprojThreshold=75.0)
    if transform is None or inliers is None or int(inliers.sum()) < 4:
        return {"status": "unverified", "reason": "landmarks_not_geometrically_consistent", "control_points": len(points), "pin_pixel": pin}
    # A low residual does not make an extrapolation trustworthy. The pin must
    # be inside the landmark hull: otherwise four matched labels on one side
    # can still produce a plausible but geographically distant point.
    hull = cv2.convexHull(pixels)
    if cv2.pointPolygonTest(hull, pin, False) < 0:
        return {"status": "unverified", "reason": "pin_outside_landmark_coverage", "control_points": len(points), "pin_pixel": pin}
    predicted = cv2.transform(pixels.reshape(-1, 1, 2), transform).reshape(-1, 2)
    residuals = np.linalg.norm(predicted - world, axis=1)
    used = inliers.astype(bool).ravel()
    rmse = float(np.sqrt(np.mean(np.square(residuals[used]))))
    max_residual = float(np.max(residuals[used]))
    pin_metres = cv2.transform(np.float32([[pin]]), transform)[0, 0]
    latitude, longitude = _unproject(float(pin_metres[0]), float(pin_metres[1]), origin_lat, origin_lon)
    verified = rmse <= MAX_ACCEPTED_RMSE_METRES and max_residual <= MAX_ACCEPTED_RESIDUAL_METRES
    result = {
        "status": "verified" if verified else "unverified",
        "reason": None if verified else "residual_exceeds_accuracy_gate",
        "rmse_metres": round(rmse, 1), "max_residual_metres": round(max_residual, 1),
        "control_points": len(points), "inlier_control_points": int(used.sum()), "pin_pixel": pin,
        "landmarks": [point.label for point in points],
    }
    # An unverified fit must not carry a coordinate. Callers have treated a
    # present latitude as permission to draw a pin.
    if verified:
        result["latitude"] = round(latitude, 6)
        result["longitude"] = round(longitude, 6)
    return result
