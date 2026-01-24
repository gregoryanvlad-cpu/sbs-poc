#!/usr/bin/env bash
set -euo pipefail

echo "[boot] running db fixups..."
python -m app.db.fixups

echo "[boot] running alembic migrations (if configured)..."
alembic upgrade head || true

echo "[boot] starting bot..."
python main.py
