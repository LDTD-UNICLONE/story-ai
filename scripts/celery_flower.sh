#!/usr/bin/env bash
set -euo pipefail

uv sync --extra monitor --frozen

uv run --frozen celery -A app.worker.celery_app flower \
  --address="${STORY_AI_FLOWER_ADDRESS:-127.0.0.1}" \
  --port="${STORY_AI_FLOWER_PORT:-5555}" \
  --basic_auth="${STORY_AI_FLOWER_BASIC_AUTH:-admin:change-me}"
