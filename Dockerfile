FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

ENV DOWNLOAD_DIR=/downloads \
    LOG_PATH=/logs/log.jsonl \
    DB_PATH=/data/app.db \
    FEEDS_DB_PATH=/data/feeds.sqlite \
    AUTO_REFRESH_INTERVAL_SECONDS=14400

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
