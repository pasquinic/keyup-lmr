# DVMProject (dvmfne / dvmhost / dvmbridge / dvmcmd) built from source, run under the KeyUp supervisor.
#
#   Central:  docker compose up -d dvm            (FNE + SIP bridges + cross-mode patches)
#   Edge:     used as the base of edge/Dockerfile (dvmhost + dvmbridge on the Pi)
#
# DVM is GPL-2.0 and is built unmodified from the official repository at DVM_REF.
# Its authors permit personal, non-commercial, hobbyist use only -- never public safety
# or life safety critical use. KeyUp will not start it until you acknowledge that in the UI.
ARG DVM_REF=435ff45b367887ffa94c89cb1a2779628e0f7bfb

FROM debian:bookworm-slim AS build
ARG DVM_REF
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates cmake g++ make libasio-dev libncurses-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/DVMProject/dvmhost.git /src \
 && cd /src && git checkout "$DVM_REF"
RUN mkdir /src/build && cd /src/build \
 && cmake .. -DENABLE_TUI_SUPPORT=off -DCMAKE_BUILD_TYPE=Release \
 && make -j"$(nproc)" dvmhost dvmfne dvmbridge dvmcmd \
 && strip dvmhost dvmfne dvmbridge dvmcmd

FROM debian:bookworm-slim AS runtime
ARG DVM_REF
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/dvmhost /src/build/dvmfne /src/build/dvmbridge /src/build/dvmcmd /usr/local/bin/
COPY --from=build /src/LICENSE /usr/share/doc/dvmhost/LICENSE
COPY lmrcommon/supervisor.py /opt/keyup/supervisor.py
LABEL org.opencontainers.image.description="DVMProject dvmhost suite @ ${DVM_REF} supervised by KeyUp"
ENV PYTHONUNBUFFERED=1
VOLUME ["/etc/dvm"]
CMD ["python3", "/opt/keyup/supervisor.py", "/etc/dvm/instances", "--bin-dir", "/usr/local/bin"]
