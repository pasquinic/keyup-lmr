"""Packet-voice channels: links that carry digital voice frames instead of audio.

A packet channel looks like a radio channel to the agent (key / unkey / cor / ptt / status), but
  * equipment -> network: it reports a call start with the talkgroup and radio ID taken from the
    packets (report_call_start), then voice frames in its native codec (report_frame), then the end;
  * network -> equipment: frames in its native codec go out untouched (write_frame); anything else
    arrives as PCM (write_audio) and is encoded with the edge's DVSI vocoder if it has one.
No local tones, announcements or CW ID are sent on packet links.
"""
from __future__ import annotations

import asyncio
import logging
import time

from lmrcommon.modes import Codec, MODE_CODEC, SystemMode
from lmrcommon.vocoder import VocoderUnavailable

from ..radio.base import RadioChannel

log = logging.getLogger("packet")


class PacketChannel(RadioChannel):
    packet = True
    interface = "packet"

    def __init__(self, cfg, loop, on_cor, on_audio, on_frame, pool):
        super().__init__(cfg, loop, on_cor, on_audio)
        self.codec: Codec = MODE_CODEC[SystemMode(cfg.get("mode", "P25P1"))]
        self.pc: dict = dict(cfg.get("packet_config") or {})
        self._on_frame = on_frame
        self.pool = pool
        self.rx_tg: int | None = None
        self.rx_emergency = False
        self.tx_info: dict = {}
        self.link_up = False
        self.enc = None
        self.enc_q: asyncio.Queue | None = None
        self.counters = {"calls_in": 0, "calls_out": 0, "frames_in": 0, "frames_out": 0, "frames_encoded": 0,
                         "frames_dropped": 0, "last_call_in": None}

    # ------------------------------------------------------------ talkgroups carried
    def carries(self, tg: int) -> bool:
        extra = str(self.cfg.get("extra_talkgroups") or "").strip()
        if tg == self.cfg.get("talkgroup_id") or extra == "*":
            return True
        return tg in {int(x) for x in extra.replace(" ", "").split(",") if x.isdigit()}

    # ------------------------------------------------------------ equipment -> agent
    def report_call_start(self, tg: int, src: int, emergency: bool = False) -> bool:
        if not self.carries(tg):
            log.info("%s: call to TG %s ignored (not carried by this link)", self.cfg.get("name"), tg)
            return False
        if self.cor:
            self.report_call_end()
        self.rx_tg, self.rx_emergency = tg, emergency
        self.counters["calls_in"] += 1
        self.counters["last_call_in"] = {"tg": tg, "src": src, "at": time.time()}
        self.report_cor(True, src)
        return True

    def report_frame(self, payload: bytes) -> None:
        if self.cor:
            self.counters["frames_in"] += 1
            self._on_frame(self.id, payload)

    def report_call_end(self) -> None:
        self.report_cor(False)

    # ------------------------------------------------------------ agent -> equipment
    def key(self) -> None:
        if self.ptt:
            return
        self.ptt = True
        self.keyed_at = time.time()
        self.counters["calls_out"] += 1
        try:
            self.begin_stream(self.tx_info)
        except Exception:
            log.exception("%s: start of stream failed", self.cfg.get("name"))

    def unkey(self) -> None:
        if not self.ptt:
            return
        self.ptt = False
        try:
            self.end_stream()
        except Exception:
            log.exception("%s: end of stream failed", self.cfg.get("name"))

    def write_frame(self, payload: bytes) -> None:
        if self.ptt:
            self.counters["frames_out"] += 1
            self.send_frame(payload)

    def write_audio(self, pcm: bytes) -> None:
        """PCM from a call in another codec: encode with the edge's vocoder, in order, off the loop."""
        if not self.ptt:
            return
        if self.enc_q is None:
            self.enc_q = asyncio.Queue(maxsize=100)
            self.loop.create_task(self._encoder())
        try:
            self.enc_q.put_nowait(pcm)
        except asyncio.QueueFull:
            self.counters["frames_dropped"] += 1

    async def _encoder(self) -> None:
        while True:
            pcm = await self.enc_q.get()
            if not self.ptt:
                continue
            try:
                if self.enc is None:
                    self.enc = self.pool.acquire(self.codec)
                data = await asyncio.to_thread(self.enc.encode, pcm)
                self.counters["frames_encoded"] += 1
                self.write_frame(data)
            except VocoderUnavailable:
                self.counters["frames_dropped"] += 1
            except Exception as e:
                log.error("%s: encode failed: %s", self.cfg.get("name"), e)
                self.counters["frames_dropped"] += 1

    async def play(self, pcm: bytes, key: bool = True) -> None:
        return   # no local tones / announcements / CW on packet links

    def abort_announcement(self) -> None:
        pass

    async def stop(self) -> None:
        self.unkey()
        if self.enc:
            self.enc.release()
            self.enc = None

    # ------------------------------------------------------------ protocol hooks
    def begin_stream(self, info: dict) -> None:
        raise NotImplementedError

    def send_frame(self, payload: bytes) -> None:
        raise NotImplementedError

    def end_stream(self) -> None:
        raise NotImplementedError

    def link_status(self) -> dict:
        return {}

    def status(self) -> dict:
        return {**super().status(), "interface": self.interface, "codec": self.codec.name, "link_up": self.link_up,
                "voice_frames_played": self.counters["frames_out"], "frames_played": self.counters["frames_out"],
                **self.counters, **self.link_status()}
