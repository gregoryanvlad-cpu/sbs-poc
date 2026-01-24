#!/usr/bin/env bash
set -euo pipefail

echo "[boot] running migrations (alembic)..."
set +e
python -m alembic upgrade head
rc=$?
set -e

if [ $rc -ne 0 ]; then
  echo "[boot] alembic upgrade failed (rc=$rc)."
  echo "[boot] stamping head to avoid crash..."
  python -m alembic stamp head || true
fi

echo "[boot] starting bot..."
python main.py
