#!/usr/bin/env bash
set -euo pipefail

echo "[boot] running migrations (alembic)..."
python -m alembic upgrade head

echo "[boot] starting bot..."
python -m app.main
