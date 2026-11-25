#!/usr/bin/env python3
"""A simulated P25 fixed station with a DFSI port, for testing KeyUp's DFSI links.

  python3 tools/dfsi_sim.py --port 15210 --api 18310

KeyUp (the host) connects to --port. Control API:
  POST /call?tg=100&src=4101&seconds=2&emergency=0   -> the station receives a radio and sends the call
  GET  /status  -> {"connected", "calls": [{"tg","src","frames","emergency","header_tg"}]}
Voice frames it sends carry a recognisable pattern (byte 0 = 40), so a fake DVSI decodes them to 1 kHz.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lmrcommon import dfsi as D  # noqa: E402
from _simhttp import serve  # noqa: E402

PATTERN = bytes([40, 80] + [0] * 9)


class Station(asyncio.DatagramProtocol):
    def __init__(self):
        self.host = None
        self.last_hb = 0.0
        self.calls: list[dict] = []
        self.cur: dict | None = None
        self.dec = D.VoiceDecoder()
        self.seq, self.ts, self.ssrc = 0, 0, random.getrandbits(32)

    def connection_made(self, tr):
        self.tr = tr

    def datagram_received(self, data, addr):
        fsc = D.parse_fsc(data)
        if fsc:
            if fsc["msg"] == "connect":
                self.host = addr
                self.tr.sendto(D.fsc_ack(D.FSC_CONNECT), addr)
            elif fsc["msg"] == "heartbeat":
                self.tr.sendto(D.fsc_heartbeat(), addr)
            self.last_hb = time.time()
            return
        rtp = D.parse_rtp(data)
        if not rtp:
            return
        for ev in self.dec.feed(rtp[2]):
            if ev[0] == "start":
                self.cur = {"tg": None, "src": None, "frames": 0, "emergency": False, "header_tg": None}
                self.calls.append(self.cur)
            elif self.cur is None:
                continue
            elif ev[0] == "header":
                self.cur["header_tg"] = ev[1]
            elif ev[0] == "lc":
                self.cur.update(tg=ev[1]["tg"], src=ev[1]["src"], emergency=ev[1]["emergency"])
            elif ev[0] == "voice":
                self.cur["frames"] += 1
            elif ev[0] == "end":
                self.cur["ended"] = True

    def rtp(self, payload, marker=False, adv=True):
        self.tr.sendto(D.rtp_packet(self.seq, self.ts, self.ssrc, payload, marker), self.host)
        self.seq += 1
        if adv:
            self.ts += 160

    async def call(self, tg, src, seconds, emergency):
        if not self.host:
            return {"error": "no host connected"}
        enc = D.VoiceEncoder(tg, src, emergency)
        self.rtp(enc.start(), True, False)
        n = int(seconds / 0.02)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(n):
            self.rtp(enc.voice(PATTERN))
            await asyncio.sleep(max(0, t0 + (i + 1) * 0.02 - loop.time()))
        self.rtp(enc.end(), adv=False)
        return {"frames": n}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--api", type=int, required=True)
    a = ap.parse_args()
    loop = asyncio.get_running_loop()
    _, st = await loop.create_datagram_endpoint(Station, local_addr=("127.0.0.1", a.port))

    async def handler(method, path, q):
        if path == "/call":
            return await st.call(int(q.get("tg", 100)), int(q.get("src", 4101)), float(q.get("seconds", 2)),
                                 q.get("emergency") == "1")
        return {"connected": bool(st.host) and time.time() - st.last_hb < 15, "calls": st.calls}
    await serve(a.api, handler)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
