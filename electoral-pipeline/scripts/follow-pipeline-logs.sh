#!/usr/bin/env bash
# Single interleaved stream: Celery worker (pipeline/DB) + OCR (per-page/card).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "Following worker + ocr (Ctrl+C to stop). Ensure stack is up: docker compose up -d"
echo "---"
exec docker compose logs -f --tail=200 worker ocr
