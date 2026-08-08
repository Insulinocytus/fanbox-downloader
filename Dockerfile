FROM ghcr.io/astral-sh/uv:0.12.2 AS uv
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser-cache \
    PATH="/app/.venv/bin:$PATH" \
    PUID=10001 \
    PGID=10001

COPY --from=uv /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        gosu \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --locked --no-dev \
    && mkdir -p /opt/cloakbrowser-cache \
    && /app/.venv/bin/python -m cloakbrowser install

COPY fanbox_dl.py .

RUN groupadd --gid 10001 appgroup \
    && useradd --create-home --uid 10001 --gid appgroup appuser \
    && mkdir -p /data/downloads \
    && chown -R appuser:appgroup /app /data /opt/cloakbrowser-cache

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data/downloads"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "fanbox_dl.py"]
