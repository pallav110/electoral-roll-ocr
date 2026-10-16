#!/usr/bin/env bash
# Server-side Phase 2 check: runs every step, tees to one log you can paste.
#
# Usage: bash tests/test_scripts/server_phase2_check.sh
#
# Prereq: on the 16-core server, repo checked out anywhere, stack currently
# running on aiml_backend. This script switches branches, rebuilds the ocr
# image, runs the comparison, and switches back.
set -u
LOG="/tmp/server_phase2_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

step() { echo; echo "================================================================"; echo "== $*"; echo "================================================================"; }

step "0. Environment"
date
git rev-parse --abbrev-ref HEAD
git log --oneline -1
nproc
free -g | head -2

step "1. Fetch and checkout aiml_improvements"
git fetch origin 2>&1 | tail -2
git checkout aiml_improvements 2>&1
git log --oneline -1

step "2. Build ocr image (first build ~3-5 min, tesserocr compiles)"
docker compose build ocr 2>&1 | tail -5

step "3. Recreate ocr with OCR_TESS_BACKEND=tesserocr"
OCR_TESS_BACKEND=tesserocr docker compose up -d --force-recreate ocr 2>&1 | tail -3

step "4. Wait for /health"
for i in $(seq 1 40); do
  H=$(curl -s --max-time 5 http://127.0.0.1:8082/health 2>/dev/null)
  if echo "$H" | grep -q '"status"'; then break; fi
  sleep 3
done
echo "$H"

step "5. Run pages 6-8 vs baseline (field diff + timing)"
# Point these at wherever the server's Phase-0 baseline JSON lives:
export OCR_BASELINE_DIR="${OCR_BASELINE_DIR:-$HOME/ocr-baseline}"
export OCR_URL="${OCR_URL:-http://127.0.0.1:8082/ocr/extract}"
export BASELINE_PDF="${BASELINE_PDF:-sample-pdfs/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf}"
echo "OCR_BASELINE_DIR=$OCR_BASELINE_DIR"
ls "$OCR_BASELINE_DIR" || echo "WARNING: baseline dir not found - adjust OCR_BASELINE_DIR"
python3 tests/test_scripts/compare_to_baseline.py server_phase2

step "6. Container memory during the run (run compare again if you want a live sample)"
docker stats --no-stream --format '{{.Name}} mem={{.MemUsage}} cpu={{.CPUPerc}}' | grep -E 'ocr|NAME' | head -3

step "7. Restore aiml_backend"
git checkout aiml_backend 2>&1
docker compose up -d --force-recreate ocr 2>&1 | tail -2
git log --oneline -1

echo
echo "DONE. Log at: $LOG"
echo "Paste the whole log back to Claude."
