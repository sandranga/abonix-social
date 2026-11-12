FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY templates ./templates
COPY assets ./assets

RUN useradd --create-home --uid 10001 social && mkdir -p /data && chown social:social /data
USER social

VOLUME ["/data"]
CMD ["python", "-m", "app.main"]
