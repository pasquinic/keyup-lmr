"""WireGuard hub management on the central server.

The central container runs with `network_mode: host` and `cap_add: NET_ADMIN`,
and manages a kernel WireGuard interface with `ip` + `wg`. Each enrolled edge
device becomes one peer with a /32 inside WG_SUBNET. The edge generates its own
private key; only its public key ever leaves the Pi.

With WG_ENABLED=false (lab/dev) the manager runs in "mock" mode: addresses are
still allocated and recorded but no kernel interface is touched.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import os
import shutil
import subprocess

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

log = logging.getLogger("wireguard")


def generate_keypair() -> tuple[str, str]:
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def valid_public_key(key: str) -> bool:
    try:
        return len(base64.b64decode(key, validate=True)) == 32
    except Exception:
        return False


class WireGuardManager:
    def __init__(self, interface: str, subnet: str, port: int, endpoint: str, data_dir: str, enabled: bool):
        self.interface = interface
        self.net = ipaddress.ip_network(subnet)
        self.server_ip = str(next(self.net.hosts()))
        self.port = port
        self.endpoint = endpoint
        self.enabled = enabled and shutil.which("wg") is not None
        if enabled and not self.enabled:
            log.warning("WG_ENABLED but `wg` binary not found -> mock mode")
        self.key_dir = os.path.join(data_dir, "wireguard")
        os.makedirs(self.key_dir, exist_ok=True)
        self.private_key, self.public_key = self._load_keys()

    # ------------------------------------------------------------------ keys
    def _load_keys(self) -> tuple[str, str]:
        pk = os.path.join(self.key_dir, "server.key")
        if not os.path.exists(pk):
            priv, _ = generate_keypair()
            with open(pk, "w") as f:
                f.write(priv)
            os.chmod(pk, 0o600)
        priv = open(pk).read().strip()
        k = X25519PrivateKey.from_private_bytes(base64.b64decode(priv))
        pub = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw)).decode()
        return priv, pub

    # ------------------------------------------------------------- interface
    def _run(self, *args: str, input: str | None = None) -> str:
        r = subprocess.run(list(args), input=input, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def ensure_up(self, peers: list[tuple[str, str]]) -> None:
        """Create/configure the interface and load all active peers [(pubkey, ip)]."""
        if not self.enabled:
            log.info("WireGuard mock mode: server %s, %d peers", self.server_ip, len(peers))
            return
        links = self._run("ip", "-o", "link", "show")
        if f": {self.interface}:" not in links:
            self._run("ip", "link", "add", "dev", self.interface, "type", "wireguard")
            self._run("ip", "address", "add", f"{self.server_ip}/{self.net.prefixlen}", "dev", self.interface)
        keyfile = os.path.join(self.key_dir, "server.key")
        self._run("wg", "set", self.interface, "listen-port", str(self.port), "private-key", keyfile)
        self._run("ip", "link", "set", "mtu", "1420", "up", "dev", self.interface)
        for pub, ip in peers:
            self.add_peer(pub, ip)
        log.info("WireGuard %s up on :%d with %d peers", self.interface, self.port, len(peers))

    def add_peer(self, pubkey: str, ip: str) -> None:
        if not self.enabled:
            return
        self._run("wg", "set", self.interface, "peer", pubkey, "allowed-ips", f"{ip}/32")

    def remove_peer(self, pubkey: str) -> None:
        if not self.enabled:
            return
        self._run("wg", "set", self.interface, "peer", pubkey, "remove")

    def allocate_ip(self, used: set[str]) -> str:
        for host in self.net.hosts():
            h = str(host)
            if h == self.server_ip or h in used:
                continue
            return h
        raise RuntimeError("WireGuard subnet exhausted")

    def peer_status(self) -> dict[str, dict]:
        """pubkey -> {endpoint, latest_handshake, rx, tx}"""
        if not self.enabled:
            return {}
        out = {}
        try:
            lines = self._run("wg", "show", self.interface, "dump").strip().splitlines()[1:]
        except RuntimeError as e:
            log.warning("%s", e)
            return {}
        for line in lines:
            p = line.split("\t")
            if len(p) >= 7:
                out[p[0]] = {"endpoint": p[2], "latest_handshake": int(p[4]), "rx_bytes": int(p[5]), "tx_bytes": int(p[6])}
        return out

    def client_config(self, ip: str) -> dict:
        return {
            "address": f"{ip}/32",
            "server_public_key": self.public_key,
            "endpoint": self.endpoint,
            "allowed_ips": f"{self.server_ip}/32",
            "server_ip": self.server_ip,
            "persistent_keepalive": 15,
        }
