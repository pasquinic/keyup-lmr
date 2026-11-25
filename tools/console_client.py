#!/usr/bin/env python3
"""Reference / test client for the KeyUp Dispatch Console protocol (services/dispatch.py), in Python.

  python3 tools/console_client.py activate --server https://lmr.example.com --code 3-ABCD-EFGH
  python3 tools/console_client.py monitor --server ... --token ... --tg 100 --tg 200 --seconds 30
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


def activate(server: str, code: str, name: str = "console-sim") -> dict:
    r = httpx.post(f"{server.rstrip('/')}/api/console/activate", json={"code": code, "name": name, "app": "sim"})
    r.raise_for_status()
    return r.json()


class ConsoleClient:
    def __init__(self, url: str, token: str, transport: str = "https"):
        self.url, self.token, self.transport = url, token, transport
        self.ws = None
        self.events: list[dict] = []
        self.rx_frames: dict[int, int] = {}
        self.rx_srcs: dict[int, set] = {}
        self.profile: dict = {}
        self._reader = None

    async def connect(self) -> dict:
        self.ws = await websockets.connect(self.url)
        await self.ws.send(json.dumps({"t": "auth", "token": self.token, "app": "sim", "machine": "lab",
                                       "transport": self.transport}))
        self.profile = json.loads(await self.ws.recv())
        if self.profile.get("t") == "auth_ok":
            self._reader = asyncio.get_running_loop().create_task(self._read())
        return self.profile

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    if m[0] == 0x02 and len(m) >= 13:
                        tg, src, _call = struct.unpack_from(">III", m, 1)
                        self.rx_frames[tg] = self.rx_frames.get(tg, 0) + (len(m) - 13) // 160
                        self.rx_srcs.setdefault(tg, set()).add(src)
                else:
                    self.events.append(json.loads(m))
        except Exception:
            pass

    async def send(self, msg: dict):
        await self.ws.send(json.dumps(msg))

    async def wait_event(self, kinds, timeout: float = 5.0, **match) -> dict:
        kinds = (kinds,) if isinstance(kinds, str) else kinds
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if e.get("t") in kinds and all(e.get(k) == v for k, v in match.items()):
                    self.events.remove(e)
                    return e
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no {kinds} {match}; got {[e.get('t') for e in self.events]}")

    async def monitor(self, tgs: list[int]) -> dict:
        await self.send({"t": "monitor", "tgs": tgs})
        return await self.wait_event("monitoring")

    async def talk(self, tg: int, seconds: float, freq: float = 600.0) -> dict:
        await self.send({"t": "ptt", "on": True, "tg": tg})
        e = await self.wait_event(("grant", "deny"))
        if e["t"] == "deny":
            return e
        n = int(seconds * 50)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(n):
            await self.ws.send(bytes([0x01]) + audio.pcm_to_ulaw(audio.tone(freq, 20, 0.4, phase0=i * 160)))
            await asyncio.sleep(max(0, t0 + (i + 1) * 0.02 - loop.time()))
        await self.send({"t": "ptt", "on": False})
        return e

    async def close(self):
        if self.ws:
            await self.ws.close()


async def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["activate", "monitor"])
    ap.add_argument("--server", required=True)
    ap.add_argument("--code")
    ap.add_argument("--token")
    ap.add_argument("--tg", type=int, action="append", default=[])
    ap.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args()
    if a.cmd == "activate":
        print(json.dumps(activate(a.server, a.code), indent=1))
        return
    url = a.server.rstrip("/").replace("http", "ws", 1) + "/console/ws"
    c = ConsoleClient(url, a.token)
    prof = await c.connect()
    print("profile:", json.dumps({k: prof.get(k) for k in ("t", "console", "unit", "zones", "rights", "reason")}))
    if prof.get("t") != "auth_ok":
        return
    await c.monitor(a.tg or [t["id"] for t in prof["talkgroups"]])
    await asyncio.sleep(a.seconds)
    print("rx frames:", c.rx_frames, "events:", c.events)
    await c.close()


if __name__ == "__main__":
    asyncio.run(_main())
