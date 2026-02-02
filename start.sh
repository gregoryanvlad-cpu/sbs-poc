#!/usr/bin/env bash
set -euo pipefail

echo "[boot] running migrations (alembic)..."

# IMPORTANT:
# Never "stamp head" on failures.
# Stamping without applying migrations can permanently desync the DB schema
# (e.g. code expects columns that were never created).

echo "[boot] schema safety check (idempotent)..."
python -m app.db.migrations.repair || true

set +e
python -m alembic upgrade head
rc=$?
set -e

if [ $rc -ne 0 ]; then
  echo "[boot] alembic upgrade failed (rc=$rc)."
  echo "[boot] refusing to start because schema may be inconsistent."
  exit $rc
fi

echo "[boot] starting bot..."
python main.py
