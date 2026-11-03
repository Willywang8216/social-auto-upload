FROM ghcr.io/astral-sh/uv:0.12.7 AS uv
FROM node:22.21.1-slim AS builder
WORKDIR /app
RUN npm config set registry https://registry.npmjs.org
COPY sau_frontend .
RUN npm ci && npm test
ENV NODE_ENV=production
ENV PATH=/app/node_modules/.bin:$PATH
RUN npm run build

# Production runtime is the base image's Python (3.12.14), asserted below.
# Base rebuilt on Python 3.12.14 and pushed to GHCR. The digest below is the
# REGISTRY MANIFEST digest (not the local image id) - rebuilding Dockerfile.base
# and pushing changes it, and this line must be updated in the same change.
FROM ghcr.io/willywang8216/sau-base:slim@sha256:d079eec1ead101cc957eedbab2cf460ab865ee5d79ed7ebace81aa8aa7684746
ARG GIT_SHA=""
ARG SAU_BUILD_REVISION=${GIT_SHA}
ARG BUILD_DATE=""
ENV SAU_BUILD_REVISION=${SAU_BUILD_REVISION}
LABEL org.opencontainers.image.revision="${SAU_BUILD_REVISION}" \
      org.opencontainers.image.created="${BUILD_DATE}"
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv

# Preserve CJK rendering and the existing writable rclone-cache mount.
# Debian ships rclone 1.60.1, which carries unpatched advisories
# (CVE-2026-41176 and CVE-2026-41179 among them). Take the upstream static
# binary instead, pinned by image digest so the build stays reproducible.
COPY --from=rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5 /usr/local/bin/rclone /usr/local/bin/rclone
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk libcups2t64 \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*
RUN rclone version
RUN python3 -c "import sys; assert sys.version_info[:3] == (3, 12, 14), 'production requires Python 3.12.14'"
COPY pyproject.toml uv.lock ./
# Export only the locked runtime graph, with hashes. A separate venv prevents
# old/unpinned dependencies in sau-base from leaking into the application.
RUN uv export --frozen --no-dev --extra web --no-emit-project --format requirements-txt --output-file /tmp/runtime.lock \
    && uv venv --python /usr/local/bin/python3 /opt/venv \
    && uv pip sync --python /opt/venv/bin/python --require-hashes /tmp/runtime.lock \
    && rm /tmp/runtime.lock \
    && rm -rf /usr/local/lib/python3.12/site-packages /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONPATH=/app

# sau-base installs its own pinned dependency list into the system interpreter.
# The app venv cannot import that environment (include-system-site-packages is
# false), but it still carried 1,295 stale distributions - old Pillow, Authlib,
# aiohttp, cryptography among them - that image scanners reported as if they
# were live. It is deleted above; this asserts the app really cannot see it.
RUN test ! -e /usr/local/lib/python3.12/site-packages \
    && python -c "import sys; assert not [p for p in sys.path if p.endswith('local/lib/python3.12/site-packages')], sys.path"
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
# Private release checkouts use umask 077. Normalize image-owned source/assets
# after all COPY steps; runtime bind mounts (including secrets) are not touched.
RUN cp conf.example.py conf.py \
    && mkdir -p /app/videoFile /app/cookiesFile /app/cookies /app/db \
    && chmod -R u=rwX,go=rX /app
EXPOSE 5409
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5409/readyz', timeout=3)"
# Web imports never own queue/maintenance work. Compose starts the SAME image
# separately as python -m myUtils.worker --max-concurrent 3.
CMD ["gunicorn", "wsgi:app", "--workers", "1", "--threads", "8", "--timeout", "1800", "--graceful-timeout", "1800", "--bind", "0.0.0.0:5409"]
