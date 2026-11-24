"""Talkgroup audio hub: local audio endpoints (SIP phones, PoC phones) <-> per-TG interconnect dvmbridge.

Each interconnect dvmbridge (see dvm.py) exchanges raw PCM16 8 kHz audio over localhost UDP:

    [pcmLength u32 BE][pcm 320 bytes, int16 LE][dstId u32 BE][srcId u32 BE]

* radio -> phones: every frame the bridge decodes from the FNE is fanned out to the
  listeners subscribed to that talkgroup; a local talker's audio is also fanned out
  locally, because the FNE does not echo a call back to the bridge it came from.
* phone -> radio: one local talker per talkgroup at a time. Frames are paced at
  20 ms through a small jitter buffer (cellular networks deliver in bursts) and
  sent with the talker's own radio ID as srcId, so the FNE applies the same RID
  rules (inhibit, rid_permitted, unknown-RID rejection) as for an RF subscriber.
  While the talker holds PTT and the buffer runs dry, silence is sent so the bridge
  does not end the call mid-sentence.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import struct
import time
from typing import Protocol

from ..db import session_scope

log = logging.getLogger("tg_audio")
FRAME = 320
SILENCE = b"\x00" * FRAME
PREBUFFER = 3          # frames (60 ms) before the first frame goes out
MAX_BUFFER = 50        # 1 s; older audio is dropped if a phone falls further behind


class Listener(Protocol):
    key: str

    def on_audio(self, tg: int, src: int, pcm: bytes) -> None: ...


class Talker:
    def __init__(self, key: str, src: int, emergency: bool):
        self.key, self.src, self.emergency = key, src, emergency
        self.queue: collections.deque[bytes] = collections.deque(maxlen=MAX_BUFFER)
        self.started = time.time()
        self.releasing = False
        self.primed = False
        self.frames = 0


class TgChannel(asyncio.DatagramProtocol):
    def __init__(self, hub: "TgAudioHub", tg: int, recv_port: int, send_port: int):
        self.hub, self.tg = hub, tg
        self.bridge = ("127.0.0.1", recv_port)
        self.send_port = send_port
        self.transport: asyncio.DatagramTransport | None = None
        self.listeners: dict[str, Listener] = {}
        self.talker: Talker | None = None
        self.rx_src = 0
        self.rx_last = 0.0

    # ---------------------------------------------------------------- radio -> listeners
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 4:
            return
        n = struct.unpack_from(">I", data)[0]
        if n == 0 or len(data) < 4 + n:
            return
        pcm = data[4:4 + n]
        src = struct.unpack_from(">I", data, 4 + n + 4)[0] if len(data) >= 4 + n + 8 else 0
        self.rx_src, self.rx_last = src, time.time()
        for lst in list(self.listeners.values()):
            if self.talker and self.talker.key == lst.key:
                continue  # don't echo a talker's own audio back
            try:
                lst.on_audio(self.tg, src, pcm)
            except Exception:
                log.exception("listener %s failed", lst.key)

    # ---------------------------------------------------------------- listeners -> radio
    def send_frame(self, pcm: bytes, src: int) -> None:
        if self.transport:
            self.transport.sendto(struct.pack(">I", len(pcm)) + pcm + struct.pack(">II", self.tg, src), self.bridge)

    def tick(self) -> None:
        t = self.talker
        if not t:
            return
        if not t.primed:
            if len(t.queue) < PREBUFFER and not t.releasing and time.time() - t.started < 0.12:
                return
            t.primed = True
        if t.queue:
            pcm = t.queue.popleft()
            self.send_frame(pcm, t.src)
            t.frames += 1
            # the FNE never echoes a call back to the peer it came from, so other phones/SIP
            # legs on this talkgroup get the talker's audio locally
            for lst in list(self.listeners.values()):
                if lst.key != t.key:
                    try:
                        lst.on_audio(self.tg, t.src, pcm)
                    except Exception:
                        log.exception("listener %s failed", lst.key)
        elif t.releasing:
            self.talker = None  # drained: the bridge ends the call after its drop time
        else:
            self.send_frame(SILENCE, t.src)


class TgAudioHub:
    def __init__(self, fne_monitor=None):
        self.fne = fne_monitor
        self.channels: dict[int, TgChannel] = {}
        self._task: asyncio.Task | None = None
        self.ports: dict[int, tuple[int, int]] = {}

    def refresh(self) -> None:
        """Re-read which talkgroups have interconnect bridges (and their ports)."""
        from .dvm import DvmPlan
        with session_scope() as db:
            plan = DvmPlan(db)
            self.ports = {t["id"]: plan.ic_ports(t["id"]) for t in plan.ic_tgs()} if plan.ack else {}
        for tg, ch in list(self.channels.items()):
            if self.ports.get(tg) != (ch.bridge[1], ch.send_port):
                if ch.transport:
                    ch.transport.close()
                del self.channels[tg]
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._pacer())

    def available(self, tg: int) -> bool:
        return tg in self.ports

    async def channel(self, tg: int) -> TgChannel | None:
        ch = self.channels.get(tg)
        if ch:
            return ch
        if tg not in self.ports:
            return None
        recv, send = self.ports[tg]
        ch = TgChannel(self, tg, recv, send)
        try:
            await asyncio.get_running_loop().create_datagram_endpoint(lambda: ch, local_addr=("127.0.0.1", send))
        except OSError as e:
            log.error("TG %d: cannot bind interconnect port %d: %s", tg, send, e)
            return None
        self.channels[tg] = ch
        log.info("TG %d interconnect <-> dvmbridge udp %d/%d", tg, recv, send)
        return ch

    async def subscribe(self, tg: int, listener: Listener) -> bool:
        ch = await self.channel(tg)
        if ch:
            ch.listeners[listener.key] = listener
        return ch is not None

    def unsubscribe(self, tg: int, key: str) -> None:
        ch = self.channels.get(tg)
        if ch:
            ch.listeners.pop(key, None)
            if ch.talker and ch.talker.key == key:
                ch.talker.releasing = True

    def unsubscribe_all(self, key: str) -> None:
        for tg in list(self.channels):
            self.unsubscribe(tg, key)

    # ---------------------------------------------------------------- floor
    def busy(self, tg: int, key: str = "") -> str | None:
        """Why the talkgroup can't be keyed right now (None = free)."""
        ch = self.channels.get(tg)
        if ch and ch.talker and ch.talker.key != key:
            return "busy"
        if ch and time.time() - ch.rx_last < 0.5:
            return "busy"  # radio traffic currently playing out of the bridge
        if self.fne is not None:
            mine = ch.talker.src if ch and ch.talker else None
            for c in self.fne.active.values():
                if c["tg"] == tg and c["src_id"] != mine:
                    return "busy"
        return None

    async def start_tx(self, tg: int, key: str, src: int, emergency: bool = False) -> str | None:
        """Returns None when granted, else a deny reason."""
        ch = await self.channel(tg)
        if ch is None:
            return "no_interconnect"
        reason = self.busy(tg, key)
        if reason and not emergency:
            return reason
        if ch.talker and ch.talker.key != key:
            if not emergency or ch.talker.emergency:
                return "busy"
            ch.talker.releasing = True  # emergency takes the local floor
        if not ch.talker or ch.talker.key != key or ch.talker.releasing:
            ch.talker = Talker(key, src, emergency)
        return None

    def push(self, tg: int, key: str, pcm: bytes) -> None:
        ch = self.channels.get(tg)
        if not ch or not ch.talker or ch.talker.key != key or ch.talker.releasing:
            return
        for i in range(0, len(pcm) - FRAME + 1, FRAME):
            ch.talker.queue.append(pcm[i:i + FRAME])

    def stop_tx(self, tg: int, key: str) -> None:
        ch = self.channels.get(tg)
        if ch and ch.talker and ch.talker.key == key:
            ch.talker.releasing = True

    def talker(self, tg: int) -> Talker | None:
        ch = self.channels.get(tg)
        return ch.talker if ch else None

    async def _pacer(self) -> None:
        loop = asyncio.get_running_loop()
        nxt = loop.time()
        while True:
            nxt += 0.02
            for ch in list(self.channels.values()):
                try:
                    ch.tick()
                except Exception:
                    log.exception("pacer tick failed for TG %d", ch.tg)
            delay = nxt - loop.time()
            if delay < -0.2:  # fell far behind (suspend/GC): resync instead of bursting
                nxt = loop.time()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    def status(self) -> dict:
        return {tg: {"listeners": len(ch.listeners), "talker": ch.talker.key if ch.talker else None}
                for tg, ch in self.channels.items()}
