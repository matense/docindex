FROM python:3.11-slim

WORKDIR /app

# Tesseract for OCR of images + curl for HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-por curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R app:app /app

USER app

ENV PORT=5000
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -f http://localhost:$PORT/login || exit 1

# The migrate step must not boot the app: importing run.py runs create_app(),
# so DOCINDEX_SKIP_STARTUP_TASKS suppresses the import-time auto_upgrade
# (which would duplicate `db upgrade heads`) and the recovery/janitor work.
# Inline env applies to the flask command only; gunicorn boots normally.
CMD DOCINDEX_SKIP_STARTUP_TASKS=1 flask --app run.py db upgrade heads && gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300 run:app
