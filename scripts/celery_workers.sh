#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen

pids=()

stop_workers() {
  for pid in "${pids[@]:-}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
}

trap stop_workers EXIT INT TERM

start_worker() {
  local name="$1"
  local queues="$2"
  local concurrency="$3"
  uv run --frozen celery -A app.worker.celery_app worker -l info -E \
    --hostname="${name}@%h" \
    --concurrency="${concurrency}" \
    --queues="${queues}" &
  pids+=("$!")
}

uv run --frozen celery -A app.worker.celery_app beat -l info \
  --schedule="${CELERY_BEAT_SCHEDULE_FILE:-celerybeat-schedule}" &
pids+=("$!")

start_worker "story-ai-default" "${CELERY_WORKER_QUEUES:-celery,story_ai_default}" "${CELERY_WORKER_CONCURRENCY:-2}"
start_worker "story-ai-text" "${CELERY_TEXT_WORKER_QUEUES:-story_ai_text}" "${CELERY_TEXT_WORKER_CONCURRENCY:-16}"
start_worker "story-ai-image" "${CELERY_IMAGE_WORKER_QUEUES:-story_ai_image}" "${CELERY_IMAGE_WORKER_CONCURRENCY:-16}"
start_worker "story-ai-video" "${CELERY_VIDEO_WORKER_QUEUES:-story_ai_video}" "${CELERY_VIDEO_WORKER_CONCURRENCY:-10}"
start_worker "story-ai-delivery" "${CELERY_DELIVERY_WORKER_QUEUES:-story_ai_delivery}" "${CELERY_DELIVERY_WORKER_CONCURRENCY:-1}"

wait
