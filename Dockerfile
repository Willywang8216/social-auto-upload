FROM node:22.21.1-bookworm AS frontend-builder

ARG VITE_API_BASE_URL=

WORKDIR /frontend

RUN npm config set registry https://registry.npmmirror.com

COPY sau_frontend/package.json ./

ENV NODE_ENV=production
ENV PATH=/frontend/node_modules/.bin:$PATH
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm install

COPY sau_frontend ./

RUN npm run build


FROM python:3.10.19-bookworm

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    fonts-dejavu-core \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libdbus-1-3 \
    libgbm1 \
    libnss3 \
    libnspr4 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    rclone \
    && rm -rf /var/lib/apt/lists/*

RUN pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

COPY requirements.txt ./requirements.txt

RUN python - <<'PY'
from pathlib import Path
src = Path('/app/requirements.txt')
dst = Path('/tmp/requirements-utf8.txt')
dst.write_text(src.read_text(encoding='utf-16'), encoding='utf-8')
PY

RUN pip install --no-cache-dir -r /tmp/requirements-utf8.txt && \
    pip install --no-cache-dir gspread==6.2.1

RUN playwright install chromium-headless-shell

COPY . .

COPY --from=frontend-builder /frontend/dist/index.html /app
COPY --from=frontend-builder /frontend/dist/assets /app/assets
COPY --from=frontend-builder /frontend/dist/vite.svg /app/assets

RUN chmod +x /app/docker/entrypoint.sh
RUN mkdir -p /app/db /app/videoFile /app/cookiesFile /app/generated_media

EXPOSE 5409

ENTRYPOINT ["/app/docker/entrypoint.sh"]
