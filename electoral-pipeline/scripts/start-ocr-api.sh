#!/usr/bin/env bash
# OCR runs in Docker Compose (service name: ocr). Use this to (re)start it alone.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec docker compose up -d --build ocr
