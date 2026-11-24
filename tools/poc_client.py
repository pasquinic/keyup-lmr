#!/usr/bin/env python3
"""Reference / test PoC client (the phone app's protocol, in Python).

  python3 tools/poc_client.py activate --server https://lmr.example.com --unit 1001 --code ABCD2345
  python3 tools/poc_client.py talk --server ... --token ... --tg 100 --seconds 3
  python3 tools/poc_client.py listen --server ... --token ... --tg 100 --seconds 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import time

import httpx
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lmrcommon import audio  # noqa: E402


def activate(server: str, unit: int, code: str, name: str = "poc-sim") -> dict:
    r = httpx.post(f"{server.rstrip('/')}/api/poc/activate",
                   json={"unit": unit, "code": code, "device_name": name, "platform": "sim", "app_version": "sim"})
    r.raise_for_status()
    return r.json()


class PocClient:
    def __init__(self, server: str, token: str):
        self.url = server.rstrip("/").replace("http", "ws", 1) + "/poc/ws"
        self.token = token
        self.ws = None
        self.events: list[dict] = []
        self.rx_frames: dict[int, int] = {}   # tg -> frames received
        self.rx_srcs: set[int] = set()
        self.profile: dict = {}
        self._reader: asyncio.Task | None = None

    async def connect(self) -> dict:
        self.ws = await websockets.connect(self.url)
        await self.ws.send(json.dumps({"t": "auth", "token": self.token, "app": "sim", "platform": "sim"}))
        self.profile = json.loads(await self.ws.recv())
        if self.profile.get("t") == "auth_ok":
            self._reader = asyncio.get_running_loop().create_task(self._read())
        return self.profile

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    if m[0] == 0x02 and len(m) >= 9:
                        tg, src = struct.unpack_from(">II", m, 1)
                        self.rx_frames[tg] = self.rx_frames.get(tg, 0) + (len(m) - 9) // 160
                        self.rx_srcs.add(src)
                else:
                    self.events.append(json.loads(m))
        except Exception:
            pass

    async def send(self, msg: dict):
        await self.ws.send(json.dumps(msg))

    async def wait_event(self, t: str, timeout: float = 5.0) -> dict:
        end = time.time() + timeout
        seen = 0
        while time.time() < end:
            for e in self.events[seen:]:
                if e.get("t") == t:
                    self.events.remove(e)
                    return e
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no {t!r} event; got {[e.get('t') for e in self.events]}")

    async def select(self, tg: int, scan: list[int] | None = None):
        await self.send({"t": "select", "tg": tg, "scan": scan or [], "scan_on": bool(scan)})
        return await self.wait_event("selected")

    async def talk(self, tg: int, seconds: float, freq: float = 700.0, emergency: bool = False) -> dict:
        await self.send({"t": "ptt", "on": True, "tg": tg, "emergency": emergency})
        while True:
            e = await self.wait_event_any(("grant", "deny"))
            if e["t"] == "deny":
                return e
            break
        # phones send in bursts over cellular: 3 frames (60 ms) per message, with jitter
        n = int(seconds * 50)
        for i in range(0, n, 3):
            frames = b"".join(audio.pcm_to_ulaw(audio.tone(freq, 20, 0.4, phase0=(i + k) * 160)) for k in range(3))
            await self.ws.send(bytes([0x01]) + frames)
            await asyncio.sleep(0.06)
        await self.send({"t": "ptt", "on": False})
        return e

    async def wait_event_any(self, kinds, timeout: float = 5.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if e.get("t") in kinds:
                    self.events.remove(e)
                    return e
            await asyncio.sleep(0.05)
        raise TimeoutError(f"none of {kinds}; got {[e.get('t') for e in self.events]}")

    async def close(self):
        if self.ws:
            await self.ws.close()


async def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["activate", "talk", "listen"])
    ap.add_argument("--server", required=True)
    ap.add_argument("--unit", type=int)
    ap.add_argument("--code")
    ap.add_argument("--token")
    ap.add_argument("--tg", type=int)
    ap.add_argument("--seconds", type=float, default=3)
    a = ap.parse_args()
    if a.cmd == "activate":
        print(json.dumps(activate(a.server, a.unit, a.code), indent=1))
        return
    c = PocClient(a.server, a.token)
    prof = await c.connect()
    print("profile:", json.dumps({k: prof.get(k) for k in ("t", "unit", "talkgroups", "reason")}))
    if prof.get("t") != "auth_ok":
        return
    await c.select(a.tg or prof["selected"])
    if a.cmd == "talk":
        print(await c.talk(a.tg or prof["selected"], a.seconds))
        await asyncio.sleep(1)
    else:
        await asyncio.sleep(a.seconds)
        print("rx frames:", c.rx_frames, "sources:", c.rx_srcs)
    print("events:", c.events)
    await c.close()


if __name__ == "__main__":
    asyncio.run(_main())
