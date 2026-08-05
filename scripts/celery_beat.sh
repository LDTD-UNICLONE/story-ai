#!/usr/bin/env bash
set -euo pipefail

uv sync --extra dev --frozen

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

exec uv run --frozen celery -A app.worker.celery_app beat -l info \
  --schedule="${CELERY_BEAT_SCHEDULE_FILE:-celerybeat-schedule}"
