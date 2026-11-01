FROM ghcr.io/astral-sh/uv:0.12.7 AS uv
FROM node:22.21.1-slim AS builder
WORKDIR /app
RUN npm config set registry https://registry.npmjs.org
COPY sau_frontend .
RUN npm ci && npm test
ENV NODE_ENV=production
ENV PATH=/app/node_modules/.bin:$PATH
RUN npm run build

# Keep the existing production Python 3.10 base (not the developer's interpreter).
FROM ghcr.io/willywang8216/sau-base:slim@sha256:f5f852b8212854b8bc09dfef9496d08a4d3894d950c141986cf380d7f778e494
ARG GIT_SHA=""
ARG SAU_BUILD_REVISION=${GIT_SHA}
ARG BUILD_DATE=""
ENV SAU_BUILD_REVISION=${SAU_BUILD_REVISION}
LABEL org.opencontainers.image.revision="${SAU_BUILD_REVISION}" \
      org.opencontainers.image.created="${BUILD_DATE}"
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv

# Preserve CJK rendering and the existing writable rclone-cache mount.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk rclone libcups2t64 \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -c "import sys; assert sys.version_info[:3] == (3, 10, 19), 'production requires Python 3.10.19'"
COPY pyproject.toml uv.lock ./
# Export only the locked runtime graph, with hashes. A separate venv prevents
# old/unpinned dependencies in sau-base from leaking into the application.
RUN uv export --frozen --no-dev --extra web --no-emit-project --format requirements-txt --output-file /tmp/runtime.lock \
    && uv venv --python /usr/local/bin/python3 /opt/venv \
    && uv pip sync --python /opt/venv/bin/python --require-hashes /tmp/runtime.lock \
    && rm /tmp/runtime.lock
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONPATH=/app
COPY . .
# The db directory is a writable data bind mount in production. Keep its
# canonical bootstrap implementation in image-owned code, never in that mount.
COPY db/createTable.py ./myUtils/db_bootstrap.py
RUN python scripts/release_checks.py static
# Install the browser matching the locked patchright, rather than assuming the
# base's cached browser revision is still the one this lock requires.
RUN python -m patchright install chromium
COPY --from=builder /app/dist /app/sau_frontend/dist
# Older routes still use these root-level SPA paths.
COPY --from=builder /app/dist/index.html /app
COPY --from=builder /app/dist/assets /app/assets
COPY --from=builder /app/dist/vite.svg /app/assets
RUN cp conf.example.py conf.py \
    && mkdir -p /app/videoFile /app/cookiesFile /app/db
EXPOSE 5409
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5409/readyz', timeout=3)"
# Web imports never own queue/maintenance work. Compose starts the SAME image
# separately as python -m myUtils.worker --max-concurrent 3.
CMD ["gunicorn", "wsgi:app", "--workers", "1", "--threads", "8", "--timeout", "1800", "--graceful-timeout", "1800", "--bind", "0.0.0.0:5409"]
