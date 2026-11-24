#!/usr/bin/env bash
# KeyUp edge installer for Raspberry Pi OS (Bookworm, 64-bit) / Ubuntu on Pi.
#
#   curl -fsSL https://lmr.example.com/edge/install.sh | sudo bash -s -- \
#        --server https://lmr.example.com --token <ENROLLMENT_TOKEN>
#
# Installs the agent to /opt/lmr-edge, writes /etc/lmr-edge/config.yaml,
# enrolls with the central server, brings up WireGuard and starts lmr-edge.service.
set -euo pipefail

SERVER=""; TOKEN=""; BACKEND="gpio"; WITH_DVM=1; GPIO_MODEM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --backend) BACKEND="$2"; shift 2;;
    --no-dvm) WITH_DVM=0; shift;;
    --gpio-modem) GPIO_MODEM=1; shift;;
    *) echo "unknown arg $1"; exit 1;;
  esac
done
[[ -n "$SERVER" && -n "$TOKEN" ]] || { echo "usage: install.sh --server URL --token TOKEN [--backend gpio|sim] [--no-dvm] [--gpio-modem]"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "==> installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev wireguard-tools libportaudio2 alsa-utils curl ca-certificates
apt-get install -y -qq python3-lgpio 2>/dev/null || true   # Pi 5 / Bookworm GPIO

if [[ $WITH_DVM -eq 1 ]] && ! command -v dvmhost >/dev/null; then
  echo "==> building the KeyUp DVM fork (dvmhost/dvmbridge) from $SERVER (20-40 min on a Pi 4)"
  apt-get install -y -qq cmake g++ make libasio-dev libncurses-dev libssl-dev
  rm -rf /opt/keyup-dvm && mkdir -p /opt/keyup-dvm
  curl -fsSL "$SERVER/edge/dvm-src.tar.gz" | tar -xz -C /opt/keyup-dvm
  mkdir -p /opt/keyup-dvm/dvmhost/build && cd /opt/keyup-dvm/dvmhost/build
  cmake .. -DENABLE_TUI_SUPPORT=off -DCMAKE_BUILD_TYPE=Release >/dev/null
  make -j"$(nproc)" dvmhost dvmbridge dvmcmd
  install -m 755 dvmhost dvmbridge dvmcmd /usr/local/bin/
  cd /
fi
# --gpio-modem: free the Pi's UART (/dev/ttyAMA0) for a DVM modem HAT on the GPIO header
if [[ $GPIO_MODEM -eq 1 ]]; then
  if [[ -f /boot/firmware/config.txt ]] && ! grep -q "dtoverlay=disable-bt" /boot/firmware/config.txt; then
    echo "dtoverlay=disable-bt" >> /boot/firmware/config.txt
    systemctl disable --now hciuart 2>/dev/null || true
    sed -i 's/console=serial0,[0-9]* //' /boot/firmware/cmdline.txt
    echo "    UART freed for the modem HAT -- reboot required"
  fi
fi

echo "==> fetching agent from $SERVER"
mkdir -p /opt/lmr-edge
curl -fsSL "$SERVER/edge/agent.tar.gz" | tar -xz -C /opt/lmr-edge
python3 -m venv --system-site-packages /opt/lmr-edge/venv
/opt/lmr-edge/venv/bin/pip install -q --upgrade pip
/opt/lmr-edge/venv/bin/pip install -q -r /opt/lmr-edge/edge/requirements.txt

mkdir -p /etc/lmr-edge /var/lib/lmr-edge
if [[ ! -f /etc/lmr-edge/config.yaml ]]; then
  sed -e "s#__SERVER__#$SERVER#" -e "s#__TOKEN__#$TOKEN#" -e "s#__BACKEND__#$BACKEND#" \
      /opt/lmr-edge/edge/config.example.yaml > /etc/lmr-edge/config.yaml
  chmod 600 /etc/lmr-edge/config.yaml
fi

echo "==> enrolling (approve the device in the web UI if the token is not auto-approve)"
cd /opt/lmr-edge/edge
PYTHONPATH=/opt/lmr-edge:/opt/lmr-edge/edge /opt/lmr-edge/venv/bin/python -m lmr_edge -c /etc/lmr-edge/config.yaml --enroll-only

install -m 644 /opt/lmr-edge/edge/systemd/lmr-edge.service /etc/systemd/system/lmr-edge.service
systemctl daemon-reload
systemctl enable --now lmr-edge.service
echo "==> done. Status: systemctl status lmr-edge ; logs: journalctl -fu lmr-edge ; local: curl localhost:8090/status"
