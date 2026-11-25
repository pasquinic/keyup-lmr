"""Central server settings, from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/lmr.db")
    data_dir: str = os.getenv("LMR_DATA_DIR", "./data")
    http_host: str = os.getenv("HTTP_HOST", "0.0.0.0")
    http_port: int = int(os.getenv("HTTP_PORT", "8080"))
    secret_key: str = os.getenv("SECRET_KEY", "change-me")
    admin_user: str = os.getenv("ADMIN_USER", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin")

    # WireGuard hub
    wg_enabled: bool = _bool("WG_ENABLED", True)
    wg_interface: str = os.getenv("WG_INTERFACE", "wg-lmr")
    wg_subnet: str = os.getenv("WG_SUBNET", "10.77.0.0/16")
    wg_port: int = int(os.getenv("WG_PORT", "51820"))
    wg_endpoint: str = os.getenv("WG_ENDPOINT", "lmr.example.com:51820")  # public host:port edges dial
    public_url: str = os.getenv("PUBLIC_URL", "https://lmr.example.com")    # enrollment URL edges use

    # Voice transport
    voice_port: int = int(os.getenv("VOICE_PORT", "50000"))
    voice_bind: str = os.getenv("VOICE_BIND", "0.0.0.0")
    # Allow edges outside the tunnel (dev/lab only). Production: false.
    allow_untunneled_edges: bool = _bool("ALLOW_UNTUNNELED_EDGES", False)

    # Transcoding (comma separated serial ports of DVSI AMBE-3000 devices)
    dvsi_ports: list[str] = field(default_factory=lambda: [p for p in os.getenv("DVSI_PORTS", "").split(",") if p])
    dvsi_baud: int = int(os.getenv("DVSI_BAUD", "460800"))

    # SIP via Asterisk ARI
    ari_enabled: bool = _bool("ARI_ENABLED", False)
    ari_url: str = os.getenv("ARI_URL", "http://127.0.0.1:8088")
    ari_user: str = os.getenv("ARI_USER", "lmr")
    ari_password: str = os.getenv("ARI_PASSWORD", "lmr")
    ari_app: str = os.getenv("ARI_APP", "lmr")
    rtp_host: str = os.getenv("RTP_HOST", "127.0.0.1")
    rtp_port_min: int = int(os.getenv("RTP_PORT_MIN", "40000"))
    rtp_port_max: int = int(os.getenv("RTP_PORT_MAX", "40999"))


settings = Settings()
