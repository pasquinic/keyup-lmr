# Optional container build for the edge (arm64). Bare-metal install.sh is recommended
# on a Pi for lowest audio latency, but this runs the same agent:
#   docker compose -f edge/docker-compose.yml up -d --build   (from repo root)
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      wireguard-tools iproute2 libportaudio2 alsa-utils gcc libc6-dev swig \
 && rm -rf /var/lib/apt/lists/*
COPY edge/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt && (pip install --no-cache-dir lgpio || true)
COPY lmrcommon /opt/lmr-edge/lmrcommon
COPY edge /opt/lmr-edge/edge
ENV PYTHONPATH=/opt/lmr-edge:/opt/lmr-edge/edge PYTHONUNBUFFERED=1
WORKDIR /opt/lmr-edge/edge
CMD ["python", "-m", "lmr_edge", "-c", "/etc/lmr-edge/config.yaml"]
