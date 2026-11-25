"""Edge agent bootstrap configuration (/etc/lmr-edge/config.yaml).

Only bootstrap + local hardware settings live here. Channel/talkgroup/subscriber
configuration is pushed from the central server and cached locally so the site
can keep working (site trunking) when central is unreachable.
"""
from __future__ import annotations

import copy
import os

import yaml

DEFAULTS = {
    "server_url": "https://lmr.example.com",   # public URL used only for enrollment
    "enrollment_token": "",
    "control_url": "",                          # override (lab); normally from enrollment (tunnel IP)
    "state_dir": "/var/lib/lmr-edge",
    "radio_backend": "gpio",                    # gpio | sim
    "wireguard": {"enabled": True, "interface": "lmr0", "config_dir": "/etc/wireguard"},
    "vocoder": {"dvsi_ports": [], "baud": 460800},
    "local_api": {"host": "127.0.0.1", "port": 8090},
    "site_trunking": {
        "timeout_s": None,              # None = use central's system setting (default 10)
        "local_repeat": True,           # cross-patch local channels on the same TG
        "post_call_tone": True,         # play site-trunking tone after each local call
        "announce_tone": True,          # play tone on all channels when entering/leaving
    },
    "notifications": {
        "data": {
            "enabled": True,
            "method": "radio",          # radio (backend send_data) | serial | none
            "targets": ["talkgroups"],  # talkgroups (group msg) and/or subscribers (individual)
            "serial_port": "/dev/ttyUSB1",
            "serial_baud": 9600,
            "template": "MSG,{target_type},{target_id},{text}\r\n",
        },
        "gpio_indicator": None,         # BCM pin driven high while site trunking (e.g. front-panel LED)
        "webhook": "",                  # optional HTTP POST on mode change (local dispatch, NMS)
    },
    "log_level": "INFO",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str) -> dict:
    data = {}
    if path and os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, data)
    os.makedirs(cfg["state_dir"], exist_ok=True)
    return cfg
