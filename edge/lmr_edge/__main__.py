"""lmr-edge entry point:  python -m lmr_edge -c /etc/lmr-edge/config.yaml"""
from __future__ import annotations

import argparse
import asyncio
import logging

from . import config as cfgmod
from .agent import EdgeAgent
from .enroll import enroll, write_wireguard
from .state import State


def main() -> None:
    ap = argparse.ArgumentParser(prog="lmr-edge")
    ap.add_argument("-c", "--config", default="/etc/lmr-edge/config.yaml")
    ap.add_argument("--enroll-only", action="store_true", help="enroll + configure WireGuard, then exit")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)
    logging.basicConfig(level=cfg.get("log_level", "INFO"), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    state = State(cfg["state_dir"])
    enroll(cfg, state)
    write_wireguard(cfg, state)
    if args.enroll_only:
        return
    asyncio.run(EdgeAgent(cfg, state).run())


if __name__ == "__main__":
    main()
