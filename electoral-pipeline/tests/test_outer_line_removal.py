from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import pytesseract

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# The photo mask is imported, not restated. This file previously carried its own
# PHOTO_BOX_REGION at (0.75, 0.23, 0.99, 0.96) and its own copy of the mask --
# a third variant of the same constant, and one that did not match the
# production value. A diagnostic that "matches production" only by intent is
# worse than no diagnostic, because its output looks authoritative.
from ocr_pdf_api import _mask_photo_box  # noqa: E402


def _card_slices(image: np.ndarray) -> list[tuple[int, int]]:
    """Return the three card spans in the supplied horizontal card strip."""
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dark = gray < 150
    projection = dark.sum(axis=0)
    peaks = np.flatnonzero(projection >= max(8, int(height * 0.90)))
    groups: list[tuple[int, int]] = []
    if len(peaks):
        start = previous = int(peaks[0])
        for value in peaks[1:]:
            value = int(value)
            if value > previous + 2:
                groups.append((start, previous + 1))
                start = value
            previous = value
        groups.append((start, previous + 1))

    # A full-height vertical card rule is a narrow group with a large projection.
    # Pair consecutive rules so the crop contains each complete card, including
    # its outer lines.  This is intentionally limited to this three-card fixture.
    vertical_edges = [
        group for group in groups
        if group[1] - group[0] <= 8
        and projection[group[0]:group[1]].max() >= int(height * 0.90)
    ]
    if len(vertical_edges) >= 6:
        # Keep the six full-height outer rules. Interior serial, EPIC, and
        # photo-box rules are shorter and were excluded by the projection test.
        return [
            (vertical_edges[index][0], vertical_edges[index + 1][1])
            for index in range(0, 6, 2)
        ]

    # Fallback for a changed rasterization: the sample is three evenly spaced cards.
    margin = max(1, round(width * 0.02))
    usable = width - 2 * margin
    return [
        (margin + round(usable * i / 3), margin + round(usable * (i + 1) / 3))
        for i in range(3)
    ]


def remove_outer_card_lines(card: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Remove only the four outer rules from one individual card crop."""
    gray = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    dark = gray < 200
    border_mask = np.zeros_like(gray, dtype=np.uint8)

    # The detector is deliberately edge-local. A Hindi shirorekha, serial box,
    # or photo-box edge is interior and cannot satisfy these full-edge ratios.
    edge_rows = min(5, height)
    edge_cols = min(5, width)
    horizontal_fraction = 0.78
    vertical_fraction = 0.86

    for y in list(range(edge_rows)) + list(range(max(0, height - edge_rows), height)):
        if np.count_nonzero(dark[y, :]) / width >= horizontal_fraction:
            border_mask[max(0, y - 1):min(height, y + 2), :] = 255

    for x in list(range(edge_cols)) + list(range(max(0, width - edge_cols), width)):
        if np.count_nonzero(dark[:, x]) / height >= vertical_fraction:
            border_mask[:, max(0, x - 1):min(width, x + 2)] = 255

    cleaned = card.copy()
    cleaned[border_mask > 0] = 255
    # Use the same photo-placeholder geometry as the production API so the
    # diagnostic cleaned card contains text only, not the printed photo box.
    cleaned = _mask_photo_box(cleaned)
    return cleaned, border_mask


def ocr_text(image: np.ndarray) -> str:
    image = _mask_photo_box(image)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    text = pytesseract.image_to_string(
        gray,
        lang="hin+eng",
        config="--oem 3 --psm 6",
    )
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path, default=Path("image_no_outer_lines.png"))
    parser.add_argument(
        "--cards-dir",
        type=Path,
        help="Directory for independent original/cleaned/mask/overlay files per card",
    )
    args = parser.parse_args()

    original = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if original is None:
        raise SystemExit(f"Could not read {args.image}")

    processed = original.copy()
    combined_mask = np.zeros(original.shape[:2], dtype=np.uint8)
    overlay = original.copy()
    spans = _card_slices(original)
    if args.cards_dir:
        args.cards_dir.mkdir(parents=True, exist_ok=True)
    total_removed = 0
    print(f"image={original.shape[1]}x{original.shape[0]} cards={spans}")
    for number, (x0, x1) in enumerate(spans, 1):
        left = max(0, x0)
        right = min(original.shape[1], x1)
        card = original[:, left:right]
        cleaned, mask = remove_outer_card_lines(card)
        processed[:, left:right] = cleaned
        combined_mask[:, left:right] = np.maximum(combined_mask[:, left:right], mask)
        overlay_card = overlay[:, left:right]
        overlay_card[mask > 0] = (0, 0, 255)
        removed_pixels = int(np.count_nonzero(mask))
        total_removed += removed_pixels
        original_text = ocr_text(card)
        cleaned_text = ocr_text(cleaned)
        if args.cards_dir:
            prefix = args.cards_dir / f"card_{number:02d}"
            cv2.imwrite(str(prefix.with_name(prefix.name + "_original.png")), card)
            cv2.imwrite(str(prefix.with_name(prefix.name + "_cleaned.png")), cleaned)
            cv2.imwrite(str(prefix.with_name(prefix.name + "_mask.png")), mask)
            card_overlay = card.copy()
            card_overlay[mask > 0] = (0, 0, 255)
            cv2.imwrite(str(prefix.with_name(prefix.name + "_overlay.png")), card_overlay)
            prefix.with_name(prefix.name + "_ocr.txt").write_text(
                f"original: {original_text}\\ncleaned: {cleaned_text}\\n",
                encoding="utf-8",
            )
        print(f"card={number} span=({left}, {right}) removed_pixels={removed_pixels}")
        print(f"card={number} original_ocr={original_text}")
        print(f"card={number} cleaned_ocr={cleaned_text}")

    cv2.imwrite(str(args.output), processed)
    mask_path = args.output.with_name(args.output.stem + "_mask.png")
    cv2.imwrite(str(mask_path), combined_mask)
    # Save the diagnostic overlay separately; red marks are the pixels removed.
    overlay_path = args.output.with_name(args.output.stem + "_overlay.png")
    cv2.imwrite(str(overlay_path), overlay)
    print(f"saved={args.output} mask={mask_path} overlay={overlay_path} total_removed_pixels={total_removed}")


if __name__ == "__main__":
    main()
