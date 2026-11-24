"""Raspberry Pi backend: GPIO COR/PTT + ALSA audio via a USB sound card.

Requires: gpiozero (with lgpio on Pi 5 / Bookworm), sounddevice (PortAudio).
Use an ALSA "plughw:" device so 8 kHz is resampled by ALSA if the card can't do it natively,
e.g. audio_device = "plughw:CARD=Device,DEV=0". One USB sound card per channel is simplest;
CM108/CM119 based interfaces (e.g. DMK URI, RA-40 style) work well.
"""
from __future__ import annotations

import logging

from lmrcommon import audio

from .base import RadioChannel

log = logging.getLogger("radio.gpio")


def _gain(pcm: bytes, db: float) -> bytes:
    if not db:
        return pcm
    g = 10 ** (db / 20)
    s = audio.pcm_to_samples(pcm)
    return audio.samples_to_pcm(max(-32768, min(32767, int(x * g))) for x in s)


class GpioAudioChannel(RadioChannel):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._cor_dev = None
        self._ptt_dev = None
        self._stream = None

    async def start(self):
        self._start_signaling()
        self._start_audio()

    def _start_signaling(self) -> None:
        from gpiozero import DigitalInputDevice, DigitalOutputDevice

        c = self.cfg
        self._cor_dev = DigitalInputDevice(c["cor_gpio"], pull_up=bool(c.get("cor_active_low", True)),
                                           bounce_time=0.03)
        self._cor_dev.when_activated = lambda: self.loop.call_soon_threadsafe(self.report_cor, True, 0)
        self._cor_dev.when_deactivated = lambda: self.loop.call_soon_threadsafe(self.report_cor, False, 0)
        self._ptt_dev = DigitalOutputDevice(c["ptt_gpio"], active_high=True, initial_value=False)
        if self._cor_dev.is_active:
            self.loop.call_soon(self.report_cor, True, 0)
        log.info("channel %s: COR gpio%s PTT gpio%s", self.id, c["cor_gpio"], c["ptt_gpio"])

    def _start_audio(self) -> None:
        import sounddevice as sd

        device = self.cfg.get("audio_device") or None
        if device == "default":
            device = None
        self._stream = sd.RawStream(samplerate=8000, channels=1, dtype="int16", blocksize=160,
                                    device=device, callback=self._cb, latency="low")
        self._stream.start()
        log.info("channel %s: audio %s", self.id, device or "default")

    def _cb(self, indata, outdata, frames, time_info, status):  # PortAudio thread
        if self.cor:
            pcm = _gain(bytes(indata), self.cfg.get("rx_gain_db", 0.0))
            self.loop.call_soon_threadsafe(self.report_audio, pcm)
        try:
            out = self.tx_queue.popleft() if self.ptt else None
        except IndexError:
            out = None
        if out:
            out = _gain(out, self.cfg.get("tx_gain_db", 0.0))
        outdata[:] = out if out and len(out) == len(outdata) else b"\x00" * len(outdata)

    def set_ptt(self, on: bool) -> None:
        super().set_ptt(on)
        if self._ptt_dev:
            self._ptt_dev.on() if on else self._ptt_dev.off()

    async def stop(self):
        self.set_ptt(False)
        for d in (self._stream,):
            if d:
                d.stop()
                d.close()
        for d in (self._cor_dev, self._ptt_dev):
            if d:
                d.close()
