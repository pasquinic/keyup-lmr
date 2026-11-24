# KeyUp's fork of DVMProject (dvm/dvmhost): dvmfne / dvmhost / dvmbridge / dvmcmd, run under the KeyUp supervisor.
#
#   Central:  docker compose up -d dvm      (FNE + interconnect bridges + cross-mode patches)
#
# GPL-2.0 (see dvm/dvmhost/LICENSE and dvm/FORK.md). DVMProject's usage guidelines say it must
# never be used for public safety or life safety; KeyUp won't start it until you acknowledge that.
FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates cmake g++ make libasio-dev libncurses-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
COPY dvm/dvmhost /src
RUN rm -rf /src/build && mkdir /src/build && cd /src/build \
 && cmake .. -DENABLE_TUI_SUPPORT=off -DCMAKE_BUILD_TYPE=Release \
 && make -j"$(nproc)" dvmhost dvmfne dvmbridge dvmcmd \
 && strip dvmhost dvmfne dvmbridge dvmcmd

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/dvmhost /src/build/dvmfne /src/build/dvmbridge /src/build/dvmcmd /usr/local/bin/
COPY dvm/dvmhost/LICENSE dvm/FORK.md /usr/share/doc/keyup-dvm/
COPY lmrcommon/supervisor.py /opt/keyup/supervisor.py
LABEL org.opencontainers.image.description="KeyUp fork of DVMProject dvmhost, supervised by KeyUp" \
      org.opencontainers.image.licenses="GPL-2.0-only"
ENV PYTHONUNBUFFERED=1
VOLUME ["/etc/dvm"]
CMD ["python3", "/opt/keyup/supervisor.py", "/etc/dvm/instances", "--bin-dir", "/usr/local/bin"]
