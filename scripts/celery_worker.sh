#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

uv run --frozen celery -A app.worker.celery_app worker -l info -E \
  --concurrency="${CELERY_WORKER_CONCURRENCY:-2}" \
  --queues="${CELERY_WORKER_QUEUES:-celery,story_ai_default}"
