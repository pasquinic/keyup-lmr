"""Site-trunking notifications to subscriber radios (and local systems).

When the edge loses central it keeps the site working and tells users they are
on "site trunking" (local-only, no wide-area talkgroup coverage):

  * data   -- a text/status message to each talkgroup (group call) and/or each
              subscriber assigned to this site (individual), sent through the
              base radio's data path (backend.send_data) or a serial data port
  * tones  -- the site-trunking alert tone on every channel, on entry and after
              each local call; a different tone on restoration
  * gpio   -- an indicator output (front-panel LED / alarm input to an NMS)
  * webhook-- optional HTTP POST for local dispatch / monitoring

On restoration a "wide area restored" message and tone are sent.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from lmrcommon import audio

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg: dict, agent):
        self.cfg = cfg
        self.agent = agent
        self._serial = None
        self._gpio = None
        self.sent: list[dict] = []
        pin = cfg["notifications"].get("gpio_indicator")
        if pin is not None and agent.cfg["radio_backend"] == "gpio":
            try:
                from gpiozero import DigitalOutputDevice
                self._gpio = DigitalOutputDevice(int(pin), initial_value=False)
            except Exception as e:
                log.warning("indicator gpio unavailable: %s", e)

    # ------------------------------------------------------------ data messages
    def _targets(self) -> list[tuple[str, int]]:
        dcfg = self.cfg["notifications"]["data"]
        out: list[tuple[str, int]] = []
        if "talkgroups" in dcfg.get("targets", []):
            tgs = sorted({c["talkgroup_id"] for c in self.agent.channels_cfg() if c.get("talkgroup_id")})
            out += [("group", tg) for tg in tgs]
        if "subscribers" in dcfg.get("targets", []):
            out += [("unit", s["id"]) for s in self.agent.cached.get("subscribers", [])
                    if s.get("home") and s.get("notify", True)]
        return out

    async def _send_serial(self, target_type: str, target_id: int, text: str) -> bool:
        dcfg = self.cfg["notifications"]["data"]
        try:
            if self._serial is None:
                import serial
                self._serial = serial.Serial(dcfg["serial_port"], dcfg.get("serial_baud", 9600), timeout=1)
            line = dcfg["template"].format(target_type=target_type, target_id=target_id, text=text)
            await asyncio.to_thread(self._serial.write, line.encode())
            return True
        except Exception as e:
            log.error("serial data send failed: %s", e)
            self._serial = None
            return False

    async def send_data(self, text: str) -> int:
        dcfg = self.cfg["notifications"]["data"]
        if not dcfg.get("enabled") or dcfg.get("method") == "none":
            return 0
        sent = 0
        radios = [r for r in self.agent.radios.values() if r.cfg.get("enabled", True)]
        for target_type, target_id in self._targets():
            ok = False
            if dcfg.get("method") == "radio":
                # send on the channel(s) carrying that TG (group) or the first channel (unit)
                chans = [r for r in radios if target_type == "unit" or r.cfg.get("talkgroup_id") == target_id] or radios[:1]
                for r in chans:
                    ok = await r.send_data(target_type, target_id, text) or ok
            elif dcfg.get("method") == "serial":
                ok = await self._send_serial(target_type, target_id, text)
            if ok:
                sent += 1
                self.sent.append({"ts": time.time(), "target_type": target_type, "target_id": target_id, "text": text})
        self.sent = self.sent[-100:]
        log.info("site-trunking data message %r sent to %d target(s)", text, sent)
        return sent

    # ------------------------------------------------------------ tones
    async def tone_all(self, kind: str) -> None:
        pcm = audio.tone_sequence(audio.TONES[kind])
        idle = [r for r in self.agent.radios.values() if not r.cor and r.cfg.get("enabled", True)]
        await asyncio.gather(*(r.play(pcm) for r in idle), return_exceptions=True)

    async def _webhook(self, event: str) -> None:
        url = self.cfg["notifications"].get("webhook")
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(url, json={"event": event, "site_id": self.agent.site_id, "ts": time.time()})
        except Exception as e:
            log.warning("webhook failed: %s", e)

    # ------------------------------------------------------------ public API
    def _msg(self, key: str, default: str) -> str:
        return self.agent.cached.get("settings", {}).get(key, default)

    async def enter(self) -> None:
        if self._gpio:
            self._gpio.on()
        st = self.cfg["site_trunking"]
        jobs = [self.send_data(self._msg("site_trunking_message", "SITE TRUNKING")), self._webhook("site_trunking")]
        if st.get("announce_tone", True):
            jobs.append(self.tone_all("site_trunking"))
        await asyncio.gather(*jobs, return_exceptions=True)

    async def periodic(self) -> None:
        await self.enter()

    async def exit(self) -> None:
        if self._gpio:
            self._gpio.off()
        st = self.cfg["site_trunking"]
        jobs = [self.send_data(self._msg("wide_area_message", "WIDE AREA RESTORED")), self._webhook("wide_area")]
        if st.get("announce_tone", True):
            jobs.append(self.tone_all("wide_area_restored"))
        await asyncio.gather(*jobs, return_exceptions=True)

    async def post_call(self, radio) -> None:
        if self.cfg["site_trunking"].get("post_call_tone", True):
            await radio.play(audio.tone_sequence(audio.TONES["site_trunking"]))
