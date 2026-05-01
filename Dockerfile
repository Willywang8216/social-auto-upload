FROM node:22.21.1 AS builder

WORKDIR /app

RUN npm config set registry https://registry.npmmirror.com

COPY sau_frontend .

RUN npm install

ENV NODE_ENV=production
ENV PATH=/app/node_modules/.bin:$PATH

RUN npm run build


FROM python:3.10.19

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update && apt-get install -y --no-install-recommends libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libasound2 && rm -rf /var/lib/apt/lists/*

RUN pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

COPY requirements.txt requirements.txt

RUN pip install -r requirements.txt

# Install browsers for both drivers. The live uploaders use patchright;
# the legacy Flask helpers still import upstream playwright. Each package
# manages its own browser bundle under PLAYWRIGHT_BROWSERS_PATH.
#
# Note: we deliberately do NOT pass --with-deps here. The system libraries
# Chromium needs (libnss3, libatk1.0-0, libgbm1, ...) are explicitly
# installed by the apt-get block earlier in this stage. --with-deps would
# additionally try to install ttf-ubuntu-font-family / ttf-unifont, which
# do not exist in Debian Trixie (this image's base distro) and break the
# build on ARM64 hosts.
RUN playwright install chromium && \
    patchright install chromium

COPY . .

COPY --from=builder /app/dist/index.html /app
COPY --from=builder /app/dist/assets /app/assets
COPY --from=builder /app/dist/vite.svg /app/assets

RUN cp conf.example.py conf.py

RUN mkdir -p /app/videoFile
RUN mkdir -p /app/cookiesFile

EXPOSE 5409

CMD ["python", "sau_backend.py"]
