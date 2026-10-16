#!/usr/bin/env bash
# Reload .env into the containers that read it.
#
# docker compose restart keeps the environment from when the container was
# created, so an edited .env does nothing until the container is recreated.
# This recreates web, the ZIP ingest worker, and the OCR worker after the
# MinIO startup check accepts the file. A rejected file leaves the running
# containers alone. A service that is already stopped stays stopped.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SERVICES=(web worker-ingest worker-ocr)
CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: scripts/apply-env.sh [--check]" >&2
  exit 2
fi

if ! docker compose run --no-deps --rm -T web python - <<'PY'
from app.config import minio_config_errors

problems = minio_config_errors()
if problems:
    print("Refusing to recreate containers.")
    print("\n".join(problems))
    raise SystemExit(1)
print("MinIO settings accepted.")
PY
then
  echo "No containers were recreated." >&2
  exit 1
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "Check only. No containers were recreated."
  exit 0
fi

running=()
for service in "${SERVICES[@]}"; do
  if [[ -n "$(docker compose ps -q --status running "$service")" ]]; then
    running+=("$service")
  else
    echo "$service is stopped. Left it stopped."
  fi
done

if [[ ${#running[@]} -eq 0 ]]; then
  echo "Nothing is running from: ${SERVICES[*]}"
  exit 0
fi

docker compose up -d --no-deps --force-recreate "${running[@]}"

echo "Containers are using:"
for service in "${running[@]}"; do
  echo "$service"
  ready=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if docker compose exec -T "$service" python -c 'import os; print("  MINIO_ENDPOINT=" + os.environ.get("MINIO_ENDPOINT", "")); print("  MINIO_CONSOLE_PUBLIC_URL=" + os.environ.get("MINIO_CONSOLE_PUBLIC_URL", ""))'; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "  $service did not stay up after recreate." >&2
    exit 1
  fi
done

echo "The minio container was not recreated. Its browser redirect changes only when that container is recreated."
