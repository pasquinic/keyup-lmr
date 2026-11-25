"""IP Site Connect / Capacity Plus link to MOTOTRBO repeaters (see lmrcommon/ipsc.py for the framing).

packet_config:
  role         master | peer    master: repeaters register to the edge (point their IPSC master
                                address at the edge); peer: the edge joins an existing master
  local_port   UDP port the edge listens on
  remote_host / remote_port     the master repeater (peer role, Capacity Plus)
  peer_id      KeyUp's IPSC peer ID (a radio ID not used by any repeater)
  auth_key     authentication key, up to 40 hex digits (blank = none); must match the repeaters
  timeslots    both | 1 | 2      which timeslots' calls KeyUp carries; KeyUp transmits on the
                                channel's "Timeslot" setting
Group voice in both directions; private calls are ignored. A call is reported with the talkgroup and
radio ID from the packet headers, and voice goes out as LC header, AMBE+2 bursts and terminator.

Capacity Plus: the edge joins the Cap+ master repeater as a peer with the Capacity Plus flag set and
takes calls from any repeater and timeslot of the system (the repeaters assign the channels).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from lmrcommon import ipsc as I

from .base import PacketChannel

log = logging.getLogger("ipsc")
KEEPALIVE_S = 5.0
PEER_TIMEOUT_S = 20.0
RX_TIMEOUT_S = 1.0


class IpscChannel(PacketChannel):
    interface = "ipsc"
    capplus = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.role = "peer" if self.capplus else (self.pc.get("role") or "master")
        self.peer_id = int(self.pc.get("peer_id") or 0)
        self.key_bytes = I.auth_key(str(self.pc.get("auth_key") or ""))
        self.flags = I.flags(bool(self.key_bytes), self.capplus)
        ts = str(self.pc.get("timeslots") or "both")
        self.rx_slots = {1, 2} if ts == "both" or self.capplus else {int(ts)}
        self.tx_slot = int(self.cfg.get("timeslot") or 1)
        self.transport = None
        self.master: tuple[str, int] | None = None
        self.master_id = 0
        self.registered = False
        self.master_heard = 0.0
        self.peers: dict[int, dict] = {}      # peer id -> {"addr", "mode", "last", "registered"}
        self.rx_stream: int | None = None
        self.rx_last = 0.0
        self.tx_stream = 0
        self.tx_seq = 0
        self.rtp_seq = random.getrandbits(16)
        self.rtp_ts = random.getrandbits(32)
        self.tx_buf: list[bytes] = []
        self.tx_burst = 0
        self.tx_info: dict = {}
        self.tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        chan = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    chan.on_datagram(data, addr)
                except Exception:
                    log.exception("%s: bad IPSC packet from %s", chan.cfg.get("name"), addr)

        port = int(self.pc.get("local_port") or 0)
        self.transport, _ = await self.loop.create_datagram_endpoint(Proto, local_addr=("0.0.0.0", port))
        if self.role == "peer" and self.pc.get("remote_host"):
            self.master = (str(self.pc["remote_host"]), int(self.pc.get("remote_port") or 50000))
        self.tasks = [self.loop.create_task(self._keepalive_loop()), self.loop.create_task(self._rx_watch())]
        log.info("%s: %s %s on udp/%s (peer id %s)%s", self.cfg.get("name"), "Capacity Plus" if self.capplus else "IPSC",
                 self.role, port, self.peer_id, f" -> master {self.master}" if self.master else "")

    async def stop(self) -> None:
        if self.transport:
            for addr in self._destinations():
                self._send(bytes([I.DE_REG_REQ]) + self.peer_id.to_bytes(4, "big"), addr)
        for t in self.tasks:
            t.cancel()
        if self.transport:
            self.transport.close()
        await super().stop()

    def _send(self, packet: bytes, addr) -> None:
        if self.transport and addr:
            self.transport.sendto(I.sign(packet, self.key_bytes), addr)

    def _destinations(self) -> list:
        out = [p["addr"] for p in self.peers.values() if p.get("registered", True)]
        if self.master and self.master not in out:
            out.append(self.master)
        return out

    # ------------------------------------------------------------ keepalive
    async def _keepalive_loop(self) -> None:
        last_list = 0.0
        while True:
            now = time.time()
            for pid, p in list(self.peers.items()):
                if now - p["last"] > PEER_TIMEOUT_S:
                    log.info("%s: peer %s timed out", self.cfg.get("name"), pid)
                    del self.peers[pid]
            if self.role == "peer" and self.master:
                if self.registered and now - self.master_heard > PEER_TIMEOUT_S:
                    log.warning("%s: master %s lost", self.cfg.get("name"), self.master)
                    self.registered = False
                if not self.registered:
                    self._send(I.master_reg_req(self.peer_id, self.flags), self.master)
                else:
                    self._send(I.master_alive_req(self.peer_id, self.flags), self.master)
                    if now - last_list > 30:
                        self._send(I.peer_list_req(self.peer_id), self.master)
                        last_list = now
                    for p in self.peers.values():
                        self._send(I.peer_alive_req(self.peer_id, self.flags) if p.get("registered")
                                   else I.peer_reg_req(self.peer_id), p["addr"])
                self.link_up = self.registered
            else:
                self.link_up = bool(self.peers)
            await asyncio.sleep(KEEPALIVE_S if self.registered or self.role == "master" else 2)

    # ------------------------------------------------------------ receive
    def on_datagram(self, data: bytes, addr) -> None:
        data = I.verify(data, self.key_bytes)
        if data is None or len(data) < 5:
            return
        op, pid = I.opcode_peer(data)
        if pid == self.peer_id:
            return
        if op in I.VOICE_OPCODES:
            if self._known(pid, addr):
                self._voice(data)
            return
        now = time.time()
        if op == I.MASTER_REG_REQ and self.role == "master":
            self.peers[pid] = {"addr": addr, "mode": data[5] if len(data) > 5 else I.MODE, "last": now, "registered": True}
            self._send(I.master_reg_reply(self.peer_id, self.flags, len(self.peers)), addr)
            log.info("%s: repeater %s registered from %s:%s", self.cfg.get("name"), pid, *addr)
        elif op == I.MASTER_ALIVE_REQ and self.role == "master" and pid in self.peers:
            self.peers[pid].update(last=now, addr=addr)
            self._send(I.master_alive_reply(self.peer_id, self.flags), addr)
        elif op == I.PEER_LIST_REQ and self.role == "master" and pid in self.peers:
            self._send(I.peer_list_reply(self.peer_id, [(p, v["addr"][0], v["addr"][1], v["mode"])
                                                        for p, v in self.peers.items()]), addr)
        elif op == I.MASTER_REG_REPLY and self.role == "peer":
            self.registered, self.master_id, self.master_heard = True, pid, now
            self._send(I.peer_list_req(self.peer_id), addr)
            log.info("%s: registered with master %s", self.cfg.get("name"), pid)
        elif op == I.MASTER_ALIVE_REPLY and self.role == "peer":
            self.master_heard = now
        elif op == I.PEER_LIST_REPLY and self.role == "peer":
            for p, ip, port, mode in I.parse_peer_list(data):
                if p not in (self.peer_id, self.master_id) and p not in self.peers:
                    self.peers[p] = {"addr": (ip, port), "mode": mode, "last": now, "registered": False}
        elif op == I.PEER_REG_REQ:
            self.peers.setdefault(pid, {"addr": addr, "mode": I.MODE, "last": now, "registered": True}).update(
                last=now, addr=addr, registered=True)
            self._send(I.peer_reg_reply(self.peer_id), addr)
        elif op == I.PEER_REG_REPLY and pid in self.peers:
            self.peers[pid].update(last=now, registered=True)
        elif op == I.PEER_ALIVE_REQ:
            if pid in self.peers:
                self.peers[pid].update(last=now, addr=addr)
            self._send(I.peer_alive_reply(self.peer_id, self.flags), addr)
        elif op == I.PEER_ALIVE_REPLY and pid in self.peers:
            self.peers[pid]["last"] = now
        elif op in (I.DE_REG_REQ,):
            self.peers.pop(pid, None)
            self._send(bytes([I.DE_REG_REPLY]) + self.peer_id.to_bytes(4, "big"), addr)

    def _known(self, pid: int, addr) -> bool:
        if pid in self.peers or pid == self.master_id:
            return True
        return bool(self.master and addr[0] == self.master[0])

    def _voice(self, data: bytes) -> None:
        v = I.parse_voice(data)
        if v is None or v["private"] or v["ts"] not in self.rx_slots:
            return
        self.rx_last = time.time()
        if v["stream"] != self.rx_stream:
            if v["burst"] == I.BURST_TERM or v["end"]:
                return   # tail of a call we never saw start
            if self.cor:
                self.report_call_end()
            self.rx_stream = v["stream"]
            if not self.report_call_start(v["dst"], v["src"], v["emergency"]):
                return
        if not self.cor:
            return
        for f in v["frames"]:
            self.report_frame(f)
        if v["burst"] == I.BURST_TERM or v["end"]:
            self.report_call_end()

    async def _rx_watch(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            if self.cor and time.time() - self.rx_last > RX_TIMEOUT_S:
                self.report_call_end()

    def report_call_end(self) -> None:
        super().report_call_end()
        # keep rx_stream so late bursts of the same call are not taken for a new call

    # ------------------------------------------------------------ transmit
    def _voice_out(self, burst: int, body: bytes, end: bool = False) -> None:
        info = self.tx_info
        pkt = I.voice_packet(self.peer_id, self.tx_seq, int(info.get("src") or 0),
                             int(info.get("tg") or self.cfg.get("talkgroup_id") or 0), self.tx_stream, self.tx_slot,
                             burst, body, self.rtp_seq, self.rtp_ts, end=end)
        self.rtp_seq = (self.rtp_seq + 1) & 0xFFFF
        for addr in self._destinations():
            self._send(pkt, addr)

    def begin_stream(self, info: dict) -> None:
        self.tx_stream = random.getrandbits(32)
        self.tx_seq = (self.tx_seq + 1) & 0xFF
        self.tx_buf, self.tx_burst = [], 0
        lc = I.full_lc(int(info.get("tg") or self.cfg.get("talkgroup_id") or 0), int(info.get("src") or 0),
                       emergency=bool(info.get("emergency")))
        self._voice_out(I.BURST_HEADER, I.lc_body(lc))

    def send_frame(self, payload: bytes) -> None:
        self.tx_buf.append(payload)
        if len(self.tx_buf) == 3:
            self._flush()

    def _flush(self) -> None:
        if not self.tx_buf:
            return
        frames, self.tx_buf = self.tx_buf, []
        self._voice_out(I.BURST_TS2 if self.tx_slot == 2 else I.BURST_TS1, I.burst_body(frames, self.tx_burst))
        self.tx_burst += 1
        self.rtp_ts = (self.rtp_ts + 480) & 0xFFFFFFFF

    def end_stream(self) -> None:
        self._flush()
        info = self.tx_info
        lc = I.full_lc(int(info.get("tg") or self.cfg.get("talkgroup_id") or 0), int(info.get("src") or 0))
        self._voice_out(I.BURST_TERM, I.lc_body(lc, terminator=True), end=True)

    def link_status(self) -> dict:
        return {"role": self.role, "registered": self.registered if self.role == "peer" else None,
                "peers": sorted(self.peers), "master": f"{self.master[0]}:{self.master[1]}" if self.master else ""}


class CapPlusChannel(IpscChannel):
    interface = "capplus"
    capplus = True
