FROM node:22.21.1-slim AS builder

WORKDIR /app

RUN npm config set registry https://registry.npmjs.org

COPY sau_frontend .

RUN npm install --legacy-peer-deps

ENV NODE_ENV=production
ENV PATH=/app/node_modules/.bin:$PATH

RUN npm run build


FROM ghcr.io/willywang8216/sau-base:latest

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

CMD ["python", "sau_backend.py"]
