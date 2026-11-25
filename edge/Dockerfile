# KeyUp edge agent + KeyUp DVM fork (dvmhost/dvmbridge) as one container (arm64 on a Raspberry Pi).
#   docker compose -f edge/docker-compose.yml up -d --build      (from the repo root)
# Bare-metal install.sh is equivalent and gives the lowest audio latency.
FROM debian:bookworm-slim AS dvm
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates cmake g++ git make libasio-dev libncurses-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
COPY dvm/dvmhost /src
RUN rm -rf /src/build && mkdir /src/build && cd /src/build \
 && cmake .. -DENABLE_TUI_SUPPORT=off -DCMAKE_BUILD_TYPE=Release \
 && make -j"$(nproc)" dvmhost dvmbridge dvmcmd && strip dvmhost dvmbridge dvmcmd

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      wireguard-tools iproute2 libportaudio2 alsa-utils libssl3 gcc libc6-dev swig \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dvm /src/build/dvmhost /src/build/dvmbridge /src/build/dvmcmd /usr/local/bin/
COPY dvm/dvmhost/LICENSE dvm/FORK.md /usr/share/doc/keyup-dvm/
COPY edge/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt && (pip install --no-cache-dir lgpio || true)
COPY lmrcommon /opt/lmr-edge/lmrcommon
COPY edge /opt/lmr-edge/edge
ENV PYTHONPATH=/opt/lmr-edge:/opt/lmr-edge/edge PYTHONUNBUFFERED=1
WORKDIR /opt/lmr-edge/edge
CMD ["python", "-m", "lmr_edge", "-c", "/etc/lmr-edge/config.yaml"]
