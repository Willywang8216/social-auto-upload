#!/bin/sh
set -eu

mkdir -p /app/db /app/videoFile /app/cookiesFile /app/generated_media /root/.config/rclone

if [ ! -f /app/conf.py ]; then
  cp /app/conf.example.py /app/conf.py
fi

python /app/db/createTable.py

exec python /app/sau_backend.py
