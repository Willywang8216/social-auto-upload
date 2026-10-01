FROM node:22.21.1-slim AS builder

WORKDIR /app

RUN npm config set registry https://registry.npmjs.org

COPY sau_frontend .

RUN npm install --legacy-peer-deps

ENV NODE_ENV=production
ENV PATH=/app/node_modules/.bin:$PATH

RUN npm run build


FROM ghcr.io/willywang8216/sau-base:slim

WORKDIR /app

# CJK fonts so ffmpeg drawtext + Pillow can render Chinese watermark/overlay
# text (e.g. Teaching's "威威教育"); the slim base only ships Latin fonts, which
# rendered CJK glyphs as tofu boxes. Placed before `COPY . .` for layer reuse.
#
# rclone is how the worker pulls media back after offload_to_drive.sh has
# moved it to Google Drive — the publish path needs it at post time, and the
# offloaded files are the ones already published once. Its config is mounted
# read-only at /app/rclone-cache.conf (see RCLONE_CONFIG in compose).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk rclone \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Fail the BUILD if any Python file does not compile under this image's own
# interpreter. The build previously never checked that the app could even be
# imported, so a syntax error that only the deployment's Python rejects shipped
# as an image and crash-looped the live container 18 times: an f-string with
# nested same-type quotes is legal on 3.12 (PEP 701) but fatal on 3.10, and the
# developer venv here is 3.12. compileall catches exactly that class in seconds,
# before the image is ever pushed.
RUN python3 -m compileall -q -x '(node_modules|sau_frontend)' . \
    || (echo "FATAL: python syntax check failed" && exit 1)

# Copy the built SPA into the exact path Flask prefers first.
COPY --from=builder /app/dist /app/sau_frontend/dist

# Keep the legacy root copies as a compatibility fallback for older routes /
# deployments that still expect /app/index.html and /app/assets.
COPY --from=builder /app/dist/index.html /app
COPY --from=builder /app/dist/assets /app/assets
COPY --from=builder /app/dist/vite.svg /app/assets

RUN cp conf.example.py conf.py

# Install any new deps not yet in the base image (idempotent)
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/videoFile
RUN mkdir -p /app/cookiesFile

EXPOSE 5409

# Production WSGI server. A single worker (with threads for concurrency) is
# used deliberately: until the publishing worker is split into its own process
# (Phase 9), the app starts in-process drain/maintenance threads, and running
# multiple Gunicorn workers would drain the job queue more than once. The
# legacy dev-server path (`python sau_backend.py`) still works for local use.
CMD ["gunicorn", "wsgi:app", "--workers", "1", "--threads", "8", "--timeout", "120", "--bind", "0.0.0.0:5409"]
