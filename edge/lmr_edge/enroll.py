"""Zero-touch enrollment and WireGuard tunnel setup."""
from __future__ import annotations

import base64
import logging
import os
import platform
import shutil
import socket
import subprocess
import time

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from . import __version__
from .state import State

log = logging.getLogger("enroll")


def _keypair() -> tuple[str, str]:
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def _pi_serial() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("Serial"):
                return line.split(":")[1].strip()
    except OSError:
        pass
    return ""


def _pi_model() -> str:
    try:
        return open("/proc/device-tree/model").read().strip("\x00\n")
    except OSError:
        return platform.machine()


def enroll(cfg: dict, state: State) -> None:
    """Enroll once with the one-time token, then wait for admin approval if required."""
    if not state.get("wg_private_key"):
        priv, pub = _keypair()
        state.set(wg_private_key=priv, wg_public_key=pub)
    url = cfg["server_url"].rstrip("/")
    verify = os.getenv("LMR_TLS_VERIFY", "true").lower() != "false"
    with httpx.Client(timeout=15, verify=verify) as http:
        if not state.enrolled:
            if not cfg.get("enrollment_token"):
                raise SystemExit("not enrolled and no enrollment_token configured")
            r = http.post(f"{url}/api/enroll", json={
                "token": cfg["enrollment_token"], "wg_public_key": state.get("wg_public_key"),
                "hostname": socket.gethostname(), "serial": _pi_serial(), "model": _pi_model(),
                "agent_version": __version__})
            if r.status_code != 200:
                raise SystemExit(f"enrollment failed: {r.status_code} {r.text}")
            d = r.json()
            state.set(device_id=d["device_id"], device_secret=d["device_secret"], site_id=d["site_id"],
                      enroll_status=d["status"], wireguard=d.get("wireguard"))
            log.info("enrolled as device %s for site %s (status %s)", d["device_id"], d["site_id"], d["status"])
        while state.get("enroll_status") != "active" or not state.get("wireguard"):
            r = http.post(f"{url}/api/enroll/status", json={"device_id": state.get("device_id"),
                                                            "device_secret": state.get("device_secret")})
            if r.status_code == 200:
                d = r.json()
                state.set(enroll_status=d["status"], wireguard=d.get("wireguard"))
                if d["status"] == "revoked":
                    raise SystemExit("device has been revoked by the administrator")
                if d["status"] == "active":
                    break
            log.info("waiting for administrator approval in the management portal...")
            time.sleep(10)


def write_wireguard(cfg: dict, state: State) -> None:
    wgc = cfg["wireguard"]
    if not wgc.get("enabled"):
        log.info("WireGuard disabled in config (lab mode)")
        return
    w = state.get("wireguard")
    conf = (
        "# managed by lmr-edge -- do not edit\n"
        "[Interface]\n"
        f"PrivateKey = {state.get('wg_private_key')}\n"
        f"Address = {w['address']}\n\n"
        "[Peer]\n"
        f"PublicKey = {w['server_public_key']}\n"
        f"Endpoint = {w['endpoint']}\n"
        f"AllowedIPs = {w['allowed_ips']}\n"
        f"PersistentKeepalive = {w.get('persistent_keepalive', 15)}\n"
    )
    iface = wgc["interface"]
    path = os.path.join(wgc["config_dir"], f"{iface}.conf")
    old = open(path).read() if os.path.exists(path) else ""
    if old != conf:
        os.makedirs(wgc["config_dir"], exist_ok=True)
        with open(path, "w") as f:
            f.write(conf)
        os.chmod(path, 0o600)
        log.info("wrote %s", path)
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "enable", f"wg-quick@{iface}"], check=False)
            subprocess.run(["systemctl", "restart", f"wg-quick@{iface}"], check=False)
        elif shutil.which("wg-quick"):
            subprocess.run(["wg-quick", "down", iface], check=False, capture_output=True)
            subprocess.run(["wg-quick", "up", iface], check=False)
