"""Simulated base radio for lab testing without hardware.

Driven through the local API:  POST /sim/key?channel=1&seconds=2&unit=1001
Transmit audio is consumed in real time and counted so tests can verify routing.
"""
from __future__ import annotations

import asyncio

from lmrcommon import audio

from .base import RadioChannel


class SimChannel(RadioChannel):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ptt_events: list[tuple[float, bool]] = []
        self.frames_played = 0
        self.voice_frames_played = 0
        self.data_messages: list[dict] = []
        self._player: asyncio.Task | None = None

    async def start(self):
        self._player = self.loop.create_task(self._play_loop())

    async def stop(self):
        if self._player:
            self._player.cancel()

    def set_ptt(self, on: bool) -> None:
        if on != self.ptt:
            self.ptt_events.append((self.loop.time(), on))
        super().set_ptt(on)

    async def _play_loop(self):
        while True:
            await asyncio.sleep(0.02)
            if self.ptt and self.tx_queue:
                f = self.tx_queue.popleft()
                self.frames_played += 1
                if audio.rms(f) > 50:
                    self.voice_frames_played += 1

    async def simulate_rx(self, seconds: float, unit: int = 0, freq: float = 1000.0) -> None:
        self.report_cor(True, unit)
        n = int(seconds * 50)
        for i in range(n):
            self.report_audio(audio.tone(freq, 20, 0.4, phase0=i * 160))
            await asyncio.sleep(0.02)
        self.report_cor(False, unit)

    async def send_data(self, target_type: str, target_id: int, text: str) -> bool:
        self.data_messages.append({"target_type": target_type, "target_id": target_id, "text": text})
        return True

    def status(self) -> dict:
        return {**super().status(), "frames_played": self.frames_played,
                "voice_frames_played": self.voice_frames_played, "ptt_events": len(self.ptt_events),
                "data_messages": self.data_messages[-20:]}
