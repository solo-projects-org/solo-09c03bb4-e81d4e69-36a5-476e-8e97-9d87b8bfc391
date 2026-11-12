FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MANIFEST_PATH=/app/manifest.json \
    DATA_DIR=/app/data \
    CLEANUP_DB_PATH=/app/data/cleanup.db \
    SAMPLE_DATA_DIR=/app/sample_data \
    SEED_SAMPLE_DATA_ON_START=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY manifest.json ./manifest.json
COPY sample_data ./sample_data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
