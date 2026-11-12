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

# The entrypoint fixes /data's owner (a host folder may belong to root) and
# then drops to the `social` user, so the app itself never runs as root.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
