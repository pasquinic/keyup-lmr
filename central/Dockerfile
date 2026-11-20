# Build from the repository root:  docker compose build central
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends wireguard-tools iproute2 iptables ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lmr
COPY central/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY lmrcommon /opt/lmr/lmrcommon
COPY edge /opt/lmr/edge
COPY central/app /opt/lmr/central/app

ENV PYTHONPATH=/opt/lmr:/opt/lmr/central \
    EDGE_SRC_DIR=/opt/lmr \
    LMR_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
WORKDIR /opt/lmr/central
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"HTTP_PORT\",\"8080\")}/healthz')"
CMD ["python", "-m", "app.main"]
