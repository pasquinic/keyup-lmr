"""SIP interconnect via Asterisk ARI + externalMedia (RTP G.711 mu-law).

Asterisk terminates SIP (phones, PBX trunks, carriers). The dialplan sends calls
into the Stasis app `lmr`; this gateway answers, bridges the caller to an
externalMedia channel whose RTP lands on a local UDP port, and turns that RTP
into talkgroup voice frames (and back).

Each SIP call becomes a SipLeg endpoint on one talkgroup:
  * inbound:  dial the talkgroup's `sip_extension` (or its TGID)
  * outbound: POST /api/sip/originate or a SipRoute with auto_connect=true

Floor control for the phone side (phones have no PTT):
  * dtmf mode: press * to key up, # to unkey
  * vox mode:  voice-activated with a hang time
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
import time

import httpx
import websockets

from lmrcommon import audio
from lmrcommon.modes import Codec
from lmrcommon.protocol import FrameType, Msg, VoiceFrame

from ..config import settings
from ..db import session_scope
from ..models import SipRoute, TalkGroup
from .call_controller import CallController, Endpoint
from .events import bus

log = logging.getLogger("sip")
RTP_HDR = struct.Struct("!BBHII")


class RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, leg: "SipLeg"):
        self.leg = leg

    def connection_made(self, transport):
        self.leg.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 12:
            return
        self.leg.peer = addr
        cc = data[0] & 0x0F
        self.leg.on_rtp(data[12 + 4 * cc:])


class SipLeg(Endpoint):
    def __init__(self, gw: "SipGateway", channel_id: str, tg: int, caller: str, ptt_mode: str, vox_threshold: int):
        self.gw = gw
        self.channel_id = channel_id
        self.key = f"sip:{channel_id}"
        self.tg = tg
        self.codec = Codec.ULAW
        self.site_id = 0
        self.label = f"SIP {caller}"
        self.caller = caller
        self.ptt_mode = ptt_mode
        self.vox_threshold = vox_threshold
        self.transport = None
        self.peer = None
        self.port = 0
        self.bridge_id = None
        self.media_id = None
        self.call_id = 0  # floor held
        self.requesting = False
        self.vox_last = 0.0
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.tx_seq = 0

    # radio -> phone
    def send_frame(self, frame: VoiceFrame) -> None:
        if not self.transport or not self.peer or not frame.payload or self.call_id:
            return
        self.seq = (self.seq + 1) & 0xFFFF
        self.ts = (self.ts + 160) & 0xFFFFFFFF
        hdr = RTP_HDR.pack(0x80, 0, self.seq, self.ts, self.ssrc)
        self.transport.sendto(hdr + frame.payload[:160], self.peer)

    # phone -> radio
    def on_rtp(self, payload: bytes) -> None:
        if self.ptt_mode == "vox" and not self.call_id and not self.requesting:
            if audio.rms(audio.ulaw_to_pcm(payload)) > self.vox_threshold:
                self.vox_last = time.time()
                asyncio.get_running_loop().create_task(self.key_up())
        if self.ptt_mode == "vox" and self.call_id and audio.rms(audio.ulaw_to_pcm(payload)) > self.vox_threshold:
            self.vox_last = time.time()
        if not self.call_id:
            return
        self.tx_seq = (self.tx_seq + 1) & 0xFFFF
        f = VoiceFrame(FrameType.VOICE, Codec.ULAW, 0, self.call_id, self.tg, 0, self.tx_seq, 0, payload)
        self.gw.ctl.route_frame(self.key, f)

    async def key_up(self) -> None:
        if self.call_id or self.requesting:
            return
        self.requesting = True
        try:
            res = await self.gw.ctl.request_call(self, {"tg": self.tg, "src": 0})
            if res.get("type") == Msg.CALL_GRANT:
                self.call_id = res["call_id"]
                self.vox_last = time.time()
                if self.ptt_mode == "vox":
                    asyncio.get_running_loop().create_task(self._vox_watch())
            else:
                await self.gw.play_tone(self.channel_id, "busy")
        finally:
            self.requesting = False

    async def dekey(self) -> None:
        if self.call_id:
            cid, self.call_id = self.call_id, 0
            await self.gw.ctl.source_ended(cid, self.key)

    async def _vox_watch(self) -> None:
        while self.call_id:
            await asyncio.sleep(0.2)
            if time.time() - self.vox_last > 1.0:
                await self.dekey()

    async def call_ended(self, call, reason):
        if call.call_id == self.call_id:
            self.call_id = 0


class SipGateway:
    def __init__(self, ctl: CallController):
        self.ctl = ctl
        self.legs: dict[str, SipLeg] = {}
        self.http = httpx.AsyncClient(base_url=f"{settings.ari_url}/ari",
                                      auth=(settings.ari_user, settings.ari_password), timeout=5)
        self.connected = False
        self._ports = set()

    async def _post(self, path: str, **params):
        r = await self.http.post(path, params=params)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _delete(self, path: str):
        try:
            await self.http.delete(path)
        except Exception:
            pass

    def _alloc_port(self) -> int:
        for p in range(settings.rtp_port_min, settings.rtp_port_max, 2):
            if p not in self._ports:
                self._ports.add(p)
                return p
        raise RuntimeError("no RTP ports free")

    async def play_tone(self, channel_id: str, kind: str) -> None:
        tones = {"busy": "tone:busy", "ok": "tone:beep"}
        try:
            await self._post(f"/channels/{channel_id}/play", media=tones.get(kind, "tone:beep"))
        except Exception:
            pass

    # ------------------------------------------------------------------ run loop
    async def run(self) -> None:
        url = settings.ari_url.replace("http", "ws", 1) + \
            f"/ari/events?app={settings.ari_app}&api_key={settings.ari_user}:{settings.ari_password}"
        while True:
            try:
                async with websockets.connect(url) as ws:
                    self.connected = True
                    log.info("connected to Asterisk ARI")
                    bus.publish("sip", connected=True)
                    asyncio.get_running_loop().create_task(self.ensure_auto_routes())
                    async for raw in ws:
                        try:
                            await self.on_event(json.loads(raw))
                        except Exception:
                            log.exception("ARI event error")
            except Exception as e:
                if self.connected:
                    bus.publish("sip", connected=False)
                self.connected = False
                log.warning("ARI connection failed (%s); retrying in 5s", e)
                await asyncio.sleep(5)

    async def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        ch = ev.get("channel", {})
        cid = ch.get("id", "")
        if t == "StasisStart":
            if ch.get("name", "").startswith("UnicastRTP/"):
                return
            await self.start_leg(ch, ev.get("args", []))
        elif t == "ChannelDtmfReceived" and cid in self.legs:
            leg = self.legs[cid]
            if ev.get("digit") == "*":
                await leg.key_up()
            elif ev.get("digit") == "#":
                await leg.dekey()
        elif t in ("StasisEnd", "ChannelDestroyed") and cid in self.legs:
            await self.stop_leg(cid)

    def _resolve_tg(self, args: list[str]) -> tuple[int | None, str, int]:
        """args: ['tg', '<tgid>'] (originate) or ['inbound', '<exten>'] (dialplan)."""
        with session_scope() as db:
            if len(args) >= 2 and args[0] == "tg":
                tg = db.get(TalkGroup, int(args[1]))
            elif len(args) >= 2:
                exten = args[1]
                tg = db.query(TalkGroup).filter(TalkGroup.sip_extension == exten).first()
                if not tg and exten.isdigit():
                    tg = db.get(TalkGroup, int(exten))
            else:
                tg = None
            if not tg or not tg.allow_sip or not tg.enabled:
                return None, "dtmf", 900
            mode, vox = (args[2] if len(args) > 2 else "dtmf"), int(args[3]) if len(args) > 3 else 900
            return tg.id, mode, vox

    async def start_leg(self, ch: dict, args: list[str]) -> None:
        cid = ch["id"]
        tg, mode, vox = self._resolve_tg(args)
        caller = ch.get("caller", {}).get("number") or ch.get("name", "?")
        if tg is None:
            log.info("SIP call %s rejected: no SIP-enabled talkgroup for %s", caller, args)
            await self._delete(f"/channels/{cid}")
            return
        leg = SipLeg(self, cid, tg, caller, mode, vox)
        loop = asyncio.get_running_loop()
        leg.port = self._alloc_port()
        await loop.create_datagram_endpoint(lambda: RtpProtocol(leg), local_addr=(settings.rtp_host, leg.port))
        try:
            await self._post(f"/channels/{cid}/answer")
            bridge = await self._post("/bridges", type="mixing", name=f"lmr-{cid}")
            leg.bridge_id = bridge["id"]
            media = await self._post("/channels/externalMedia", app=settings.ari_app,
                                     external_host=f"{settings.rtp_host}:{leg.port}", format="ulaw")
            leg.media_id = media["id"]
            await self._post(f"/bridges/{leg.bridge_id}/addChannel", channel=f"{cid},{leg.media_id}")
        except Exception as e:
            log.error("failed to set up SIP leg %s: %s", cid, e)
            await self.stop_leg(cid, leg)
            return
        self.legs[cid] = leg
        self.ctl.sip_legs[leg.key] = leg
        call = self.ctl.calls.get(self.ctl.tg_calls.get(tg, -1))
        if call:  # join an in-progress call
            call.dests[leg.key] = []
        await self.play_tone(cid, "ok")
        bus.publish("sip_leg", action="up", tg=tg, caller=caller)
        log.info("SIP leg %s (%s) joined TG %d [%s]", cid, caller, tg, mode)

    async def stop_leg(self, cid: str, leg: SipLeg | None = None) -> None:
        leg = leg or self.legs.pop(cid, None)
        self.legs.pop(cid, None)
        if not leg:
            return
        await leg.dekey()
        self.ctl.sip_legs.pop(leg.key, None)
        for call in self.ctl.calls.values():
            call.dests.pop(leg.key, None)
        if leg.media_id:
            await self._delete(f"/channels/{leg.media_id}")
        if leg.bridge_id:
            await self._delete(f"/bridges/{leg.bridge_id}")
        if leg.transport:
            leg.transport.close()
        self._ports.discard(leg.port)
        bus.publish("sip_leg", action="down", tg=leg.tg, caller=leg.caller)

    async def originate(self, endpoint: str, tg: int, ptt_mode: str = "dtmf", vox: int = 900,
                        caller_id: str = "LMR") -> dict:
        return await self._post("/channels", endpoint=endpoint, app=settings.ari_app,
                                appArgs=f"tg,{tg},{ptt_mode},{vox}", callerId=caller_id)

    async def ensure_auto_routes(self) -> None:
        await asyncio.sleep(2)
        with session_scope() as db:
            routes = [r.to_dict() for r in db.query(SipRoute).filter(SipRoute.auto_connect.is_(True),
                                                                     SipRoute.enabled.is_(True))]
        active_tgs = {leg.tg for leg in self.legs.values()}
        for r in routes:
            if r["talkgroup_id"] not in active_tgs:
                try:
                    await self.originate(r["endpoint"], r["talkgroup_id"], r["ptt_mode"], r["vox_threshold"])
                except Exception as e:
                    log.warning("auto route %s failed: %s", r["name"], e)

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected,
                "legs": [{"channel": l.channel_id, "caller": l.caller, "tg": l.tg, "keyed": bool(l.call_id),
                          "ptt_mode": l.ptt_mode} for l in self.legs.values()]}
