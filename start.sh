#!/usr/bin/env bash
set -euo pipefail

echo "[boot] running migrations (alembic)..."
set +e
python -m alembic upgrade head
rc=$?
set -e

if [ $rc -ne 0 ]; then
  echo "[boot] alembic upgrade failed (rc=$rc)."
  echo "[boot] If DB already has tables, stamping head to avoid crash..."
  # Mark current DB as being at latest revision, so app can start without dropping DB.
  python -m alembic stamp head || true
fi

echo "[boot] starting bot..."
python -m app.main
