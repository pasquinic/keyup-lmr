#!/usr/bin/env python3
"""A simulated MOTOTRBO repeater on IP Site Connect, for testing KeyUp's IPSC links.

  # repeater registering to KeyUp acting as IPSC master:
  python3 tools/ipsc_sim.py --peer-id 311001 --port 15201 --master 127.0.0.1:15200 --api 18301 [--auth-key 1A2B]
  # repeater acting as master that KeyUp joins as a peer:
  python3 tools/ipsc_sim.py --peer-id 311009 --port 15209 --be-master --api 18309

Control API: POST /call?tg=9&src=3101&seconds=2&ts=1&emergency=0  -> sends a group voice call
             GET  /status  -> {"registered", "peers", "calls": [{"tg","src","ts","frames","emergency","from"}]}
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
from lmrcommon import ipsc as I  # noqa: E402
from _simhttp import serve  # noqa: E402

PATTERN = bytes([40, 80, 0, 0, 0, 0, 0])


class Repeater(asyncio.DatagramProtocol):
    def __init__(self, a):
        self.a = a
        self.key = I.auth_key(a.auth_key)
        self.fl = I.flags(bool(self.key), a.capplus)
        self.master = None
        if a.master:
            h, p = a.master.split(":")
            self.master = (h, int(p))
        self.registered = False
        self.peers: dict[int, tuple] = {}
        self.calls: list[dict] = []
        self.cur: dict[int, dict] = {}

    def connection_made(self, tr):
        self.tr = tr

    def send(self, pkt, addr):
        self.tr.sendto(I.sign(pkt, self.key), addr)

    def datagram_received(self, data, addr):
        data = I.verify(data, self.key)
        if data is None:
            return
        op, pid = I.opcode_peer(data)
        if op == I.MASTER_REG_REPLY:
            self.registered = True
            self.peers[pid] = addr
        elif op == I.MASTER_REG_REQ and self.a.be_master:
            self.peers[pid] = addr
            self.send(I.master_reg_reply(self.a.peer_id, self.fl, len(self.peers)), addr)
        elif op == I.MASTER_ALIVE_REQ and self.a.be_master:
            self.send(I.master_alive_reply(self.a.peer_id, self.fl), addr)
        elif op == I.PEER_LIST_REQ and self.a.be_master:
            self.send(I.peer_list_reply(self.a.peer_id, [(p, ad[0], ad[1], I.MODE) for p, ad in self.peers.items()]), addr)
        elif op in (I.PEER_REG_REQ, I.PEER_ALIVE_REQ):
            self.peers[pid] = addr
            self.send(I.peer_reg_reply(self.a.peer_id) if op == I.PEER_REG_REQ else I.peer_alive_reply(self.a.peer_id, self.fl), addr)
        elif op in I.VOICE_OPCODES:
            v = I.parse_voice(data)
            c = self.cur.get(v["stream"])
            if c is None:
                c = self.cur[v["stream"]] = {"tg": v["dst"], "src": v["src"], "ts": v["ts"], "frames": 0,
                                             "emergency": v["emergency"], "from": v["peer"], "header": False}
                self.calls.append(c)
            if v["burst"] == I.BURST_HEADER:
                c["header"], c["emergency"] = True, v["emergency"]
            c["frames"] += len(v["frames"])
            if v["end"]:
                c["ended"] = True

    async def keepalive(self):
        while True:
            if self.master:
                self.send(I.master_alive_req(self.a.peer_id, self.fl) if self.registered
                          else I.master_reg_req(self.a.peer_id, self.fl), self.master)
            await asyncio.sleep(1 if not self.registered else 5)

    async def call(self, tg, src, seconds, ts, emergency):
        stream, seq, rts = random.getrandbits(32), random.getrandbits(8), 0
        dests = list(self.peers.values()) or ([self.master] if self.master else [])

        def out(burst, body, end=False):
            nonlocal seq
            pkt = I.voice_packet(self.a.peer_id, 1, src, tg, stream, ts, burst, body, seq, rts, end=end)
            seq += 1
            for d in dests:
                self.send(pkt, d)
        out(I.BURST_HEADER, I.lc_body(I.full_lc(tg, src, emergency=emergency)))
        n = int(seconds / 0.06)
        for i in range(n):
            out(I.BURST_TS2 if ts == 2 else I.BURST_TS1, I.burst_body([PATTERN] * 3, i))
            rts += 480
            await asyncio.sleep(0.06)
        out(I.BURST_TERM, I.lc_body(I.full_lc(tg, src), terminator=True), end=True)
        return {"bursts": n, "frames": 3 * n}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-id", type=int, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--master")
    ap.add_argument("--be-master", action="store_true")
    ap.add_argument("--capplus", action="store_true")
    ap.add_argument("--auth-key", default="")
    ap.add_argument("--api", type=int, required=True)
    a = ap.parse_args()
    loop = asyncio.get_running_loop()
    _, rep = await loop.create_datagram_endpoint(lambda: Repeater(a), local_addr=("127.0.0.1", a.port))
    loop.create_task(rep.keepalive())

    async def handler(method, path, q):
        if path == "/call":
            return await rep.call(int(q.get("tg", 9)), int(q.get("src", 3101)), float(q.get("seconds", 2)),
                                  int(q.get("ts", 1)), q.get("emergency") == "1")
        return {"registered": rep.registered or a.be_master, "peers": sorted(rep.peers), "calls": rep.calls}
    await serve(a.api, handler)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
