"""Radio channel interface: one base station radio wired to the Pi.

Wiring per channel (typical):
  * RX audio  (discriminator/speaker-level or line out) -> USB sound card input
  * TX audio  USB sound card output -> radio mic/line-in (accessory connector)
  * COR / COS (carrier/squelch detect) -> GPIO input via opto-isolator
  * PTT       GPIO output -> transistor/opto -> radio PTT line

The backend reports COR transitions and 20 ms PCM frames (8 kHz, 16-bit) while
COR is active, and plays queued PCM frames while PTT is keyed.
"""
from __future__ import annotations

import asyncio
import collections
import time
from typing import Callable

from lmrcommon import audio


class RadioChannel:
    def __init__(self, cfg: dict, loop: asyncio.AbstractEventLoop,
                 on_cor: Callable[[int, bool, int], None], on_audio: Callable[[int, bytes], None]):
        self.cfg = cfg
        self.id: int = cfg["id"]
        self.loop = loop
        self._on_cor = on_cor
        self._on_audio = on_audio
        self.cor = False
        self.ptt = False
        self.tx_queue: collections.deque[bytes] = collections.deque(maxlen=250)  # 5 s
        self._unkey_task: asyncio.Task | None = None
        self.keyed_at = 0.0
        self.cw_keyed = False  # PTT is held for the CW station ID
        self.announcing = False  # PTT is held only for a local tone/announcement

    # -- lifecycle
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.set_ptt(False)

    def update_config(self, cfg: dict) -> None:
        self.cfg = cfg

    # -- events to the agent (always called on the event loop)
    def report_cor(self, active: bool, unit: int = 0) -> None:
        if active == self.cor:
            return
        self.cor = active
        self._on_cor(self.id, active, unit)

    def report_audio(self, pcm: bytes) -> None:
        self._on_audio(self.id, pcm)

    # -- transmit
    def set_ptt(self, on: bool) -> None:
        self.ptt = on

    def key(self) -> None:
        """Key the transmitter; leading silence covers the radio's TX delay."""
        if self._unkey_task:
            self._unkey_task.cancel()
            self._unkey_task = None
        self.announcing = False
        if not self.ptt:
            self.tx_queue.clear()
            self.set_ptt(True)
            self.keyed_at = time.time()
            for _ in range(max(0, self.cfg.get("tx_delay_ms", 150)) // 20):
                self.tx_queue.append(audio.silence())

    def unkey(self) -> None:
        if not self.ptt or self._unkey_task:
            return

        async def _tail():
            # let queued audio drain, then hold for the tail time
            while self.tx_queue:
                await asyncio.sleep(0.02)
            await asyncio.sleep(self.cfg.get("tx_tail_ms", 200) / 1000)
            self.set_ptt(False)
            self._unkey_task = None

        self._unkey_task = self.loop.create_task(_tail())

    def write_audio(self, pcm: bytes) -> None:
        if self.ptt:
            self.tx_queue.append(pcm)

    async def play(self, pcm: bytes, key: bool = True) -> None:
        """Play a locally generated sound (tone/announcement), keying if needed."""
        was_keyed = self.ptt
        if key and not was_keyed:
            self.key()
            self.announcing = True
        for f in audio.split_frames(pcm):
            self.tx_queue.append(f)
        if key and not was_keyed:
            self.unkey()
        while self.tx_queue and self.ptt:
            await asyncio.sleep(0.02)
        if not was_keyed:
            self.announcing = False

    def abort_announcement(self) -> None:
        """A user keyed up while we were sending a tone: stop and release PTT at once."""
        if self._unkey_task:
            self._unkey_task.cancel()
            self._unkey_task = None
        self.tx_queue.clear()
        self.announcing = False
        self.set_ptt(False)

    # -- data (radio/repeater specific; override in a backend that supports it)
    async def send_data(self, target_type: str, target_id: int, text: str) -> bool:
        return False

    def status(self) -> dict:
        return {"id": self.id, "name": self.cfg.get("name"), "cor": self.cor, "ptt": self.ptt,
                "tg": self.cfg.get("talkgroup_id"), "mode": self.cfg.get("mode")}
