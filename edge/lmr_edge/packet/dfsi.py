"""DFSI link to a P25 fixed station (see lmrcommon/dfsi.py for the framing).

packet_config: local_port (UDP port the edge listens on), remote_host / remote_port (the station's
DFSI port), heartbeat_s (default 5). KeyUp connects (FSC_CONNECT), then both sides send heartbeats;
three missed heartbeats drop the link and KeyUp reconnects. A station may also connect first.

Inbound: a call is reported to the network once its link control (talkgroup + radio ID, voice frames
3-8 of the first LDU1) has arrived; the frames before it are held and sent right after, so nothing
is lost. Outbound: KeyUp sends start of stream, the voice header, IMBE frames with link control /
encryption sync spread over the superframe, and end of stream.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from lmrcommon import dfsi as D

from .base import PacketChannel

log = logging.getLogger("dfsi")
RX_TIMEOUT_S = 1.0


class DfsiChannel(PacketChannel):
    interface = "dfsi"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.transport = None
        self.remote = None
        self.hb = float(self.pc.get("heartbeat_s") or 5)
        self.last_heard = 0.0
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.encoder: D.VoiceEncoder | None = None
        self.decoder = D.VoiceDecoder()
        self.held: list[bytes] = []
        self.rx_last = 0.0
        self.rx_header_tg: int | None = None
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        chan = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                chan.on_datagram(data, addr)

        port = int(self.pc.get("local_port") or 0)
        self.transport, _ = await self.loop.create_datagram_endpoint(Proto, local_addr=("0.0.0.0", port))
        if self.pc.get("remote_host"):
            self.remote = (str(self.pc["remote_host"]), int(self.pc.get("remote_port") or port))
        self.tasks = [self.loop.create_task(self._link_loop()), self.loop.create_task(self._rx_watch())]
        log.info("%s: DFSI on udp/%s -> %s", self.cfg.get("name"), port, self.remote)

    async def stop(self) -> None:
        if self.transport and self.remote and self.link_up:
            self.transport.sendto(D.fsc_disconnect(), self.remote)
        for t in self.tasks:
            t.cancel()
        if self.transport:
            self.transport.close()
        await super().stop()

    # ------------------------------------------------------------ control link
    async def _link_loop(self) -> None:
        while True:
            now = time.time()
            if self.link_up and now - self.last_heard > 3 * self.hb:
                log.warning("%s: DFSI link lost (no heartbeat)", self.cfg.get("name"))
                self.link_up = False
            if self.remote:
                if not self.link_up:
                    self.transport.sendto(D.fsc_connect(int(self.pc.get("local_port") or 0), self.ssrc,
                                                        int(self.hb), int(self.hb)), self.remote)
                else:
                    self.transport.sendto(D.fsc_heartbeat(), self.remote)
            await asyncio.sleep(self.hb if self.link_up else min(self.hb, 2))

    def on_datagram(self, data: bytes, addr) -> None:
        fsc = D.parse_fsc(data)
        if fsc is not None:
            self.last_heard = time.time()
            m = fsc["msg"]
            if m == "connect":                       # the station connected first
                self.remote = (addr[0], fsc["vc_port"] or addr[1])
                self.transport.sendto(D.fsc_ack(D.FSC_CONNECT), addr)
                self._up()
            elif m == "ack" and fsc["acked"] == D.FSC_CONNECT and fsc["code"] == D.ACK_OK:
                self._up()
            elif m == "heartbeat" and not self.link_up and self.remote and addr[0] == self.remote[0]:
                self._up()
            elif m == "disconnect":
                self.link_up = False
            return
        if self.remote and addr[0] != self.remote[0]:
            return   # voice only from the connected station
        rtp = D.parse_rtp(data)
        if rtp is None:
            return
        self.last_heard = self.rx_last = time.time()
        for ev in self.decoder.feed(rtp[2]):
            self._on_event(ev)

    def _up(self) -> None:
        if not self.link_up:
            log.info("%s: DFSI link up with %s", self.cfg.get("name"), self.remote)
        self.link_up = True

    # ------------------------------------------------------------ inbound voice
    def _on_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "start":
            if self.cor:
                self.report_call_end()
            self.held, self.rx_header_tg = [], None
        elif kind == "header":
            self.rx_header_tg = ev[1]
        elif kind == "lc":
            lc = ev[1]
            if not self.cor and self.report_call_start(lc["tg"], lc["src"], lc["emergency"]):
                for f in self.held:
                    self.report_frame(f)
            self.held = []
        elif kind == "voice":
            if self.cor:
                self.report_frame(ev[1])
            else:
                self.held = (self.held + [ev[1]])[-18:]
        elif kind == "end":
            self.held = []
            if self.cor:
                self.report_call_end()

    async def _rx_watch(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            if self.cor and time.time() - self.rx_last > RX_TIMEOUT_S:
                self.report_call_end()   # lost end of stream

    # ------------------------------------------------------------ outbound voice
    def _send_rtp(self, payload: bytes, marker: bool = False, advance: bool = True) -> None:
        if not (self.transport and self.remote):
            return
        self.transport.sendto(D.rtp_packet(self.seq, self.ts, self.ssrc, payload, marker), self.remote)
        self.seq = (self.seq + 1) & 0xFFFF
        if advance:
            self.ts = (self.ts + 160) & 0xFFFFFFFF

    def begin_stream(self, info: dict) -> None:
        tg = int(info.get("tg") or self.cfg.get("talkgroup_id") or 0)
        nac = int(str(self.cfg.get("nac") or "293"), 16)
        self.encoder = D.VoiceEncoder(tg, int(info.get("src") or 0), bool(info.get("emergency")), nac)
        self._send_rtp(self.encoder.start(), marker=True, advance=False)

    def send_frame(self, payload: bytes) -> None:
        if self.encoder:
            self._send_rtp(self.encoder.voice(payload))

    def end_stream(self) -> None:
        if self.encoder:
            self._send_rtp(self.encoder.end(), advance=False)
            self.encoder = None

    def link_status(self) -> dict:
        return {"remote": f"{self.remote[0]}:{self.remote[1]}" if self.remote else "",
                "heard_s_ago": round(time.time() - self.last_heard, 1) if self.last_heard else None}
