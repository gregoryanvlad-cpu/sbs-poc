# Hotfix: ensure start.sh is used as entrypoint so DB fixups run on Railway
# Keep your existing build steps above if you have them; this patch provides a safe baseline.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (optional but helpful for some wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements*.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt || true
RUN if [ -f /app/requirements.patch.txt ]; then pip install --no-cache-dir -r /app/requirements.patch.txt; fi

COPY . /app

# IMPORTANT: make sure the script is executable inside the container
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
