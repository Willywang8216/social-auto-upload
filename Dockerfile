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

COPY . .

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
