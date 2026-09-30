#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen

# Only read the new worker concurrency keys; never source or evaluate .env.
read -r query_concurrency media_concurrency < <(uv run --frozen python - <<'PYCONFIG'
import os
from dotenv import dotenv_values
values = dotenv_values(".env")
result = []
for key, default in (("CELERY_QUERY_WORKER_CONCURRENCY", "4"), ("CELERY_MEDIA_WORKER_CONCURRENCY", "2")):
    value = os.environ.get(key, values.get(key) or default)
    if not value.isdecimal() or int(value) < 1:
        raise SystemExit(f"{key} must be a positive integer")
    result.append(str(int(value)))
print(" ".join(result))
PYCONFIG
)

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
  local max_tasks="${4:-50}"
  uv run --frozen celery -A app.worker.celery_app worker -l info -E \
    --hostname="${name}@%h" \
    --concurrency="${concurrency}" \
    --queues="${queues}" --max-tasks-per-child="${max_tasks}" &
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

start_worker "story-ai-query" "story_ai_query" "${query_concurrency}" 1000
start_worker "story-ai-media" "story_ai_media" "${media_concurrency}"

wait
