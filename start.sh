#!/usr/bin/env sh
set -eu

echo "[boot] running migrations..."
alembic upgrade head

echo "[boot] starting app..."
python main.py
