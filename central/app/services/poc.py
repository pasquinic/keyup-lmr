"""Push-to-talk over cellular (PoC) gateway.

A phone running the KeyUp app is a subscriber radio: it uses the radio ID of a Subscriber record and
is held to the same rules as an RF radio, because its transmissions go through the router's own floor
control (CallController.request_call) with that radio ID:
  * inhibit:      a disabled subscriber is refused / disconnected at once
  * talkgroups:   only permitted talkgroups (empty list = any), restricted TGs only if listed,
                  arranged in the radio ID's zones
  * floor:        busy when the talkgroup (or a patched one) is in use; emergency may take the floor
  * timeout:      the system's maximum call time
  * last seen:    updates the subscriber's last talkgroup / time like a radio would
Phone audio is G.711 mu-law, the router's analog codec, so phones, analog sites and SIP need no
transcoding; digital sites are reached through the router's vocoders.

Protocol (WebSocket /poc/ws, JSON text frames + binary audio), see docs/poc-protocol.md:
  client -> server  {"t":"auth","token","app","platform"}   {"t":"select","tg","scan":[..],"scan_on"}
                    {"t":"ptt","on":true,"tg","emergency"}  {"t":"ptt","on":false}
                    {"t":"emergency","tg","on"}             {"t":"ping","ts"}
                    binary 0x01 + N*160 bytes G.711 mu-law (20 ms frames, 8 kHz)
  server -> client  auth_ok / profile / auth_fail / inhibited / grant / deny / tx_end /
                    rx_start / rx_end / emergency / status / pong
                    binary 0x02 + tg(u32 BE) + src(u32 BE) + 160 bytes mu-law
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import struct
import time

from lmrcommon.modes import Codec
from lmrcommon.protocol import FrameType, Msg, VoiceFrame

from ..db import session_scope
from ..models import PocDevice, Subscriber, now
from .call_controller import Endpoint, get_system_settings
from .events import bus
from .zones import unit_view

log = logging.getLogger("poc")
PROTO = 1
AUDIO_UP, AUDIO_DOWN = 0x01, 0x02
FRAME = 160
RX_IDLE_S = 0.6


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


class PocSession(Endpoint):
    def __init__(self, gw: "PocGateway", ws, device: dict, unit: dict):
        self.gw, self.ws = gw, ws
        self.device_id = device["id"]
        self.platform = device.get("platform", "")
        self.unit = unit
        self.key = f"poc:{device['id']}:{secrets.token_hex(3)}"
        self.codec = Codec.ULAW
        self.site_id = 0
        self.label = f"PoC {unit.get('alias') or unit['id']}"
        self.talkgroups: dict[int, dict] = {}
        self.zones: list[dict] = []
        self.selected: int | None = None
        self.scan: list[int] = []
        self.scan_on = False
        self.subscribed: set[int] = set()
        self.tx_tg: int | None = None
        self.tx_call = 0
        self.tx_seq = 0
        self.tx_started = 0.0
        self.tx_emergency = False
        self.rx_tg: int | None = None
        self.rx_src = 0
        self.rx_call = 0
        self.rx_last = 0.0
        self.connected_at = time.time()
        self.send_lock = asyncio.Lock()
        self.closed = False

    @property
    def unit_id(self) -> int:
        return self.unit["id"]

    async def send(self, msg: dict) -> None:
        if self.closed:
            return
        try:
            async with self.send_lock:
                await self.ws.send_text(json.dumps(msg))
        except Exception:
            self.closed = True

    async def _send_bytes(self, data: bytes) -> None:
        try:
            async with self.send_lock:
                await self.ws.send_bytes(data)
        except Exception:
            self.closed = True

    def _spawn(self, coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    # ------------------------------------------------------------ router endpoint
    def listens(self, group: frozenset) -> bool:
        return bool(self.subscribed & group)

    def send_frame(self, frame: VoiceFrame) -> None:
        if self.closed or not frame.payload or frame.type == FrameType.KEEPALIVE or self.tx_tg is not None:
            return  # half duplex, like a radio
        group = self.gw.ctl.linked(frame.tg) | {frame.tg}
        mine = sorted(self.subscribed & group)
        if not mine:
            return
        tg = frame.tg if frame.tg in mine else (self.selected if self.selected in mine else mine[0])
        t = time.time()
        idle = t - self.rx_last > RX_IDLE_S
        if self.rx_tg is not None and tg != self.rx_tg and not idle:
            # priority: the selected talkgroup interrupts scanned traffic
            if not (tg == self.selected and self.rx_tg != self.selected):
                return
        if self.rx_tg != tg or self.rx_src != frame.src or self.rx_call != frame.call_id or idle:
            self.rx_tg, self.rx_src, self.rx_call = tg, frame.src, frame.call_id
            call = self.gw.ctl.calls.get(frame.call_id)
            self._spawn(self.send({
                "t": "rx_start", "tg": tg, "tg_name": self.talkgroups.get(tg, {}).get("name", str(tg)),
                "src": frame.src, "alias": self.gw.alias(frame.src), "emergency": bool(call and call.emergency)}))
        self.rx_last = t
        payload = frame.payload
        for i in range(0, len(payload) - FRAME + 1, FRAME):
            self._spawn(self._send_bytes(struct.pack(">BII", AUDIO_DOWN, tg, frame.src) + payload[i:i + FRAME]))

    async def call_ended(self, call, reason):
        if call.call_id == self.tx_call and self.tx_tg is not None:   # our own call: max time, admin, ...
            await self.gw.dekey(self, reason, notify_router=False)
        elif call.call_id == self.rx_call and self.rx_tg is not None:
            tg, self.rx_tg = self.rx_tg, None
            await self.send({"t": "rx_end", "tg": tg})

    async def preempted(self, call):
        if call.call_id == self.tx_call:
            await self.gw.dekey(self, "preempted", notify_router=False)

    def info(self) -> dict:
        return {"key": self.key, "unit": self.unit_id, "alias": self.unit.get("alias"), "device_id": self.device_id,
                "platform": self.platform, "selected": self.selected, "scan": self.scan if self.scan_on else [],
                "talking": self.tx_tg, "connected_s": round(time.time() - self.connected_at)}


class PocGateway:
    def __init__(self, ctl):
        self.ctl = ctl
        self.sessions: dict[str, PocSession] = {}
        self.emergency: dict[int, dict] = {}

    def alias(self, unit: int) -> str:
        return self.ctl.unit_alias(unit) if unit else "Unknown"

    # ------------------------------------------------------------------ rules
    def _load(self, device_id: int) -> tuple[dict | None, dict | None, dict, str]:
        """(device, unit, view, deny_reason) straight from the database."""
        with session_scope() as db:
            dev = db.get(PocDevice, device_id)
            if not dev or dev.revoked:
                return None, None, {}, "device_revoked"
            sub = db.get(Subscriber, dev.subscriber_id)
            if not sub:
                return None, None, {}, "unknown_unit"
            if not sub.enabled:
                return dev.to_dict(), sub.to_dict(), {}, "inhibited"
            if not sub.poc_enabled:
                return dev.to_dict(), sub.to_dict(), {}, "poc_disabled"
            return dev.to_dict(), sub.to_dict(), unit_view(db, sub, poc=True), ""

    def authenticate(self, token: str) -> tuple[dict | None, dict | None, dict, str]:
        with session_scope() as db:
            dev = db.query(PocDevice).filter(PocDevice.token_hash == token_hash(token or "-")).first()
            if not dev:
                return None, None, {}, "invalid_token"
            dev.last_seen = now()
            dev_id = dev.id
        return self._load(dev_id)

    def _apply_view(self, s: PocSession, view: dict) -> None:
        s.talkgroups = {t["id"]: t for t in view.get("talkgroups", [])}
        s.zones = view.get("zones", [])
        if s.selected not in s.talkgroups:
            s.selected = next(iter(s.talkgroups), None)
        s.scan = [t for t in s.scan if t in s.talkgroups]

    def _profile(self, s: PocSession, kind: str) -> dict:
        with session_scope() as db:
            sysset = get_system_settings(db)
        return {"t": kind, "proto": PROTO, "unit": {"id": s.unit_id, "alias": s.unit.get("alias")},
                "system": {"name": sysset.get("system_name", "KeyUp"), "max_call_s": int(sysset.get("max_call_s", 120))},
                "talkgroups": list(s.talkgroups.values()), "zones": s.zones, "selected": s.selected,
                "scan": s.scan, "scan_on": s.scan_on, "network": self.network_ok(), "server_time": time.time()}

    def network_ok(self) -> bool:
        return True   # the phone talks to central itself; radio sites join through the router

    async def _resubscribe(self, s: PocSession) -> None:
        want = set()
        if s.selected in s.talkgroups:
            want.add(s.selected)
        if s.scan_on:
            want |= {t for t in s.scan if t in s.talkgroups}
        s.subscribed = want
        if not s.closed:
            await self.ctl.attach_leg(s)

    # ------------------------------------------------------------------ websocket
    async def handle(self, ws) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except Exception:
            await ws.close(code=4000)
            return
        dev, unit, view, reason = self.authenticate(first.get("token", "")) if first.get("t") == "auth" \
            else (None, None, {}, "expected_auth")
        if reason:
            await ws.send_text(json.dumps({"t": "auth_fail", "reason": reason}))
            await ws.close(code=4003)
            log.info("PoC auth refused: %s", reason)
            return
        s = PocSession(self, ws, dev, unit)
        self._apply_view(s, view)
        with session_scope() as db:
            d = db.get(PocDevice, dev["id"])
            d.platform, d.app_version = str(first.get("platform", ""))[:16], str(first.get("app", ""))[:16]
        self.sessions[s.key] = s
        await self._resubscribe(s)
        await s.send(self._profile(s, "auth_ok"))
        bus.publish("poc", action="online", unit=s.unit_id, alias=unit.get("alias"), platform=s.platform)
        log.info("PoC unit %s (%s) online from %s", s.unit_id, unit.get("alias"), s.platform or "?")
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    self._on_audio(s, msg["bytes"])
                elif msg.get("text") is not None:
                    await self._on_message(s, json.loads(msg["text"]))
        except Exception as e:
            log.debug("PoC session %s ended: %s", s.key, e)
        finally:
            await self._close(s)

    async def _close(self, s: PocSession) -> None:
        if self.sessions.get(s.key) is not s:
            return
        del self.sessions[s.key]
        s.closed = True
        await self.ctl.detach_leg(s)
        s.tx_tg = None
        bus.publish("poc", action="offline", unit=s.unit_id, alias=s.unit.get("alias"))

    def _on_audio(self, s: PocSession, data: bytes) -> None:
        if not data or data[0] != AUDIO_UP or s.tx_tg is None or not s.tx_call:
            return
        body = data[1:]
        flags = 1 if s.tx_emergency else 0
        for i in range(0, len(body) - FRAME + 1, FRAME):
            s.tx_seq = (s.tx_seq + 1) & 0xFFFF
            self.ctl.route_frame(s.key, VoiceFrame(FrameType.VOICE, Codec.ULAW, 0, s.tx_call, s.tx_tg, s.unit_id,
                                                   s.tx_seq, flags, body[i:i + FRAME]))

    async def _on_message(self, s: PocSession, m: dict) -> None:
        t = m.get("t")
        if t == "ping":
            await s.send({"t": "pong", "ts": m.get("ts"), "network": self.network_ok()})
        elif t == "select":
            tg = m.get("tg")
            if tg is not None and int(tg) in s.talkgroups and s.tx_tg is None:
                s.selected = int(tg)
            if "scan" in m:
                s.scan = [int(x) for x in m.get("scan") or [] if int(x) in s.talkgroups][:16]
            if "scan_on" in m:
                s.scan_on = bool(m["scan_on"])
            await self._resubscribe(s)
            self._seen(s.unit_id, s.selected)
            await s.send({"t": "selected", "tg": s.selected, "scan": s.scan, "scan_on": s.scan_on})
        elif t == "ptt":
            if m.get("on"):
                await self._key(s, int(m.get("tg") or s.selected or 0), bool(m.get("emergency")))
            else:
                await self.dekey(s, "released")
        elif t == "emergency":
            await self._emergency(s, int(m.get("tg") or s.selected or 0), bool(m.get("on", True)))

    async def _key(self, s: PocSession, tg: int, emergency: bool) -> None:
        if s.tx_tg is not None:
            return
        reason = None
        if tg not in s.talkgroups:
            reason = "not_permitted"
        elif emergency and not s.talkgroups[tg].get("emergency"):
            emergency = False
        res = None
        if not reason:
            res = await self.ctl.request_call(s, {"tg": tg, "src": s.unit_id, "emergency": emergency})
            if res.get("type") != Msg.CALL_GRANT:
                reason = res.get("reason", "denied")
        if reason:
            await s.send({"t": "deny", "tg": tg, "reason": reason})
            return
        s.tx_tg, s.tx_call, s.tx_seq = tg, res["call_id"], 0
        s.tx_started, s.tx_emergency = time.time(), emergency
        s.rx_tg = None
        await s.send({"t": "grant", "tg": tg, "emergency": emergency})

    async def dekey(self, s: PocSession, reason: str, notify_router: bool = True) -> None:
        if s.tx_tg is None:
            return
        tg, call_id = s.tx_tg, s.tx_call
        s.tx_tg, s.tx_call = None, 0
        if notify_router and call_id:
            await self.ctl.source_ended(call_id, s.key)
        await s.send({"t": "tx_end", "tg": tg, "reason": reason})

    async def _emergency(self, s: PocSession, tg: int, on: bool) -> None:
        if tg not in s.talkgroups or not s.talkgroups[tg].get("emergency"):
            return
        self.emergency[tg] = {"on": on, "src": s.unit_id, "ts": time.time()}
        await self.broadcast_emergency(tg, s.unit_id, s.unit.get("alias"), on)
        bus.publish("emergency", tg=tg, unit=s.unit_id, alias=s.unit.get("alias"), on=on, via="poc")
        log.warning("EMERGENCY %s on TG %s by unit %s (%s)", "declared" if on else "cleared", tg, s.unit_id,
                    s.unit.get("alias"))

    async def broadcast_emergency(self, tg: int, unit: int, alias: str | None, on: bool) -> None:
        evt = {"t": "emergency", "tg": tg, "src": unit, "alias": alias, "on": on}
        for other in list(self.sessions.values()):
            if tg in other.talkgroups:
                await other.send(evt)

    def _seen(self, unit: int, tg: int | None) -> None:
        with session_scope() as db:
            sub = db.get(Subscriber, unit)
            if sub:
                sub.last_seen, sub.last_site_id = now(), 0
                if tg:
                    sub.last_tg_id = tg

    # ------------------------------------------------------------------ housekeeping
    async def refresh(self) -> None:
        """Re-apply rules after an admin change (inhibit, permissions, zones, revocation)."""
        for s in list(self.sessions.values()):
            dev, unit, view, reason = self._load(s.device_id)
            if reason:
                await s.send({"t": "inhibited" if reason == "inhibited" else "auth_fail", "reason": reason})
                await self.dekey(s, reason)
                try:
                    await s.ws.close(code=4003)
                except Exception:
                    pass
                await self._close(s)
                continue
            s.unit = unit
            self._apply_view(s, view)
            if s.tx_tg is not None and s.tx_tg not in s.talkgroups:
                await self.dekey(s, "not_permitted")
            await self._resubscribe(s)
            await s.send(self._profile(s, "profile"))

    async def run(self) -> None:
        """Watchdog: rx_end after silence, drop dead sessions."""
        while True:
            await asyncio.sleep(0.2)
            t = time.time()
            for s in list(self.sessions.values()):
                if s.closed:
                    await self._close(s)
                    continue
                if s.rx_tg is not None and t - s.rx_last > RX_IDLE_S:
                    tg, s.rx_tg = s.rx_tg, None
                    await s.send({"t": "rx_end", "tg": tg})

    def status(self) -> dict:
        return {"sessions": [s.info() for s in self.sessions.values()],
                "emergencies": {tg: e for tg, e in self.emergency.items() if e.get("on")}}
