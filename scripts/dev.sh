#!/usr/bin/env bash
set -euo pipefail

uv sync --extra dev --frozen

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

uv run --frozen uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
