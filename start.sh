#!/usr/bin/env bash
set -euo pipefail

echo "[start] running DB fixups (idempotent)..."
python -m app.db.fixups || (echo "[start] DB fixups failed" && exit 1)

# If Alembic is present, apply migrations too (safe if already up-to-date).
if [ -f "alembic.ini" ]; then
  echo "[start] running alembic upgrade head..."
  alembic upgrade head
fi

echo "[start] starting app..."
python main.py
