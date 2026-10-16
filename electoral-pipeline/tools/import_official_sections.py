#!/usr/bin/env python3
"""Validate a JSON catalogue before enabling OFFICIAL_SECTION_CATALOG_PATH."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from app.official_sections import _load_catalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    args = parser.parse_args()
    rows = _load_catalog(str(args.catalog), args.catalog.stat().st_mtime_ns)
    if not rows:
        raise SystemExit("No valid official section rows.")
    names = [row["source_filename"] for row in rows]
    if len(names) != len(set(names)):
        raise SystemExit("Duplicate source_filename rows; resolve ambiguity first.")
    print(json.dumps({"valid_rows": len(rows), "status": "safe_to_enable"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
