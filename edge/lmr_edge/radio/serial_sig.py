"""Radio interface using a serial port's modem-control lines instead of GPIO.

Common with USB radio interface cables (FTDI / CP210x): RTS drives the radio's PTT
and CTS reads its COR/COS. Audio still goes through the channel's sound card.
"""
from __future__ import annotations

import logging
import threading
import time

from .gpio import GpioAudioChannel

log = logging.getLogger("radio.serial")
POLL_S = 0.01
DEBOUNCE_S = 0.03


class SerialSignalChannel(GpioAudioChannel):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._port = None
        self._stop = threading.Event()

    def _start_signaling(self) -> None:
        import serial

        port = self.cfg.get("serial_port") or ""
        if not port:
            raise RuntimeError("serial signalling selected but no serial_port configured")
        self._port = serial.Serial(port, 9600, timeout=0)
        self._port.rts = False
        self._port.dtr = True  # many interface cables power their opto-isolators from DTR
        threading.Thread(target=self._watch_cts, daemon=True, name=f"cts-{self.id}").start()
        log.info("channel %s: PTT=RTS COR=CTS on %s", self.id, port)

    def _carrier(self) -> bool:
        cts = bool(self._port.cts)
        return not cts if self.cfg.get("cor_invert") else cts

    def _watch_cts(self) -> None:
        state, since = None, 0.0
        while not self._stop.is_set():
            try:
                now = self._carrier()
            except Exception as e:  # cable unplugged
                log.error("channel %s: serial read failed: %s", self.id, e)
                time.sleep(1)
                continue
            if now != state:
                if since == 0.0:
                    since = time.monotonic()
                elif time.monotonic() - since >= DEBOUNCE_S:
                    state, since = now, 0.0
                    self.loop.call_soon_threadsafe(self.report_cor, now, 0)
            else:
                since = 0.0
            time.sleep(POLL_S)

    def set_ptt(self, on: bool) -> None:
        super(GpioAudioChannel, self).set_ptt(on)
        if self._port:
            self._port.rts = on

    async def stop(self):
        self._stop.set()
        await super().stop()
        if self._port:
            try:
                self._port.rts = False
                self._port.close()
            except Exception:
                pass
