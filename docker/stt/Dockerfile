# Local STT + spoken-language detection for the SAU pipeline.
#
# Why a separate image instead of a dependency of the app image:
#   * the app image is deployed by ci-watch on every push; a 400 MB
#     CTranslate2/model layer would be re-pulled on every deploy;
#   * this service is CPU-heavy (whisper on 4 cores) and must not compete with
#     the app's ffmpeg transcodes for the same cgroup;
#   * the app already speaks an HTTP STT contract, so a sidecar is free.
#
# It cannot run on the *host* and be reached from the app: the host firewall
# drops traffic from the docker bridges to host ports (verified: the container
# cannot connect to 172.17.0.1:9099 or 172.18.0.1:9099, both of which answer
# from the host itself). A container on the same network is the only path.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pin av to a release whose API faster-whisper 1.2.x actually calls
# (`av.open(..., metadata_errors=...)` was removed in av 14).
RUN pip install "faster-whisper==1.2.1" "av<14"

# Bake the model into the image so a restart never depends on huggingface.co
# being reachable (the whole point of this service is to survive an outage).
ARG STT_MODEL=tiny
RUN python -c "\
from faster_whisper import WhisperModel; \
WhisperModel('${STT_MODEL}', device='cpu', compute_type='int8')" \
 && find /models -name '*.bin' -o -name '*.txt' | head

COPY scripts/local_stt_server.py /app/local_stt_server.py

# Run as the host operator's uid so the cache directory is writable, matching
# the app container's convention.
ARG SAU_UID=1000
ARG SAU_GID=1000
RUN mkdir -p /cache && chown -R ${SAU_UID}:${SAU_GID} /cache
USER ${SAU_UID}:${SAU_GID}

ENV SAU_STT_LOCAL_CACHE=/cache \
    SAU_STT_LOCAL_PORT=9099 \
    SAU_STT_LOCAL_MODEL=${STT_MODEL}

EXPOSE 9099
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9099/healthz',timeout=5)"

CMD ["python", "/app/local_stt_server.py"]
