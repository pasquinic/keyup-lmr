"""Push-to-talk over cellular (PoC) gateway.

A phone running the KeyUp app is a subscriber radio: it uses the radio ID of a
Subscriber record and is held to the same rules as an RF radio --

  * inhibit:      a disabled subscriber is refused / disconnected immediately
  * talkgroups:   only its permitted talkgroups (empty list = any), restricted TGs only if listed
  * RID rules:    transmissions carry its radio ID into the FNE (interconnect dvmbridge
                  source-ID override), so rid_acl / rid_permitted / unknown-RID rules apply
  * floor:        busy when the talkgroup is in use (radio or phone); emergency may take the floor
  * timeout:      the system's maximum call time
  * last seen:    updates the subscriber's last talkgroup / time like a radio would

Protocol (WebSocket /poc/ws, JSON text frames + binary audio):
  client -> server  {"t":"auth","token","app","platform"}   {"t":"select","tg","scan":[..],"scan_on"}
                    {"t":"ptt","on":true,"tg","emergency"}  {"t":"ptt","on":false}
                    {"t":"emergency","tg","on"}             {"t":"ping","ts"}
                    binary 0x01 + N*160 bytes G.711 mu-law (20 ms frames, 8 kHz)
  server -> client  auth_ok / profile / auth_fail / inhibited / grant / deny / tx_end /
                    rx_start / rx_end / emergency / status / pong
                    binary 0x02 + tg(u32 BE) + src(u32 BE) + 160 bytes mu-law
See docs/poc-protocol.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import struct
import time

from lmrcommon import audio

from ..db import session_scope
from ..models import PocDevice, Subscriber, now
from .events import bus

log = logging.getLogger("poc")
PROTO = 1
AUDIO_UP, AUDIO_DOWN = 0x01, 0x02
RX_IDLE_S = 0.6


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


class PocSession:
    def __init__(self, gw: "PocGateway", ws, device: dict, unit: dict):
        self.gw, self.ws = gw, ws
        self.device_id = device["id"]
        self.platform = device.get("platform", "")
        self.unit = unit
        self.key = f"poc:{device['id']}:{secrets.token_hex(3)}"
        self.talkgroups: dict[int, dict] = {}
        self.selected: int | None = None
        self.scan: list[int] = []
        self.scan_on = False
        self.subscribed: set[int] = set()
        self.tx_tg: int | None = None
        self.tx_started = 0.0
        self.tx_emergency = False
        self.rx_tg: int | None = None
        self.rx_src = 0
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

    def send_audio_nowait(self, tg: int, src: int, ulaw: bytes) -> None:
        if self.closed:
            return
        asyncio.get_running_loop().create_task(self._send_bytes(struct.pack(">BII", AUDIO_DOWN, tg, src) + ulaw))

    async def _send_bytes(self, data: bytes) -> None:
        try:
            async with self.send_lock:
                await self.ws.send_bytes(data)
        except Exception:
            self.closed = True

    # tg_audio.Listener ------------------------------------------------------------
    def on_audio(self, tg: int, src: int, pcm: bytes) -> None:
        if self.tx_tg is not None:
            return  # half duplex, like a radio
        t = time.time()
        idle = t - self.rx_last > RX_IDLE_S
        if self.rx_tg is not None and tg != self.rx_tg and not idle:
            # priority: the selected talkgroup interrupts scanned traffic
            if not (tg == self.selected and self.rx_tg != self.selected):
                return
        if self.rx_tg != tg or self.rx_src != src or idle:
            self.rx_tg, self.rx_src = tg, src
            asyncio.get_running_loop().create_task(self.send({
                "t": "rx_start", "tg": tg, "tg_name": self.talkgroups.get(tg, {}).get("name", str(tg)),
                "src": src, "alias": self.gw.alias(src), "emergency": self.gw.emergency.get(tg, {}).get("on", False)}))
        self.rx_last = t
        self.send_audio_nowait(tg, src, audio.pcm_to_ulaw(pcm))

    def info(self) -> dict:
        return {"key": self.key, "unit": self.unit_id, "alias": self.unit.get("alias"), "device_id": self.device_id,
                "platform": self.platform, "selected": self.selected, "scan": self.scan if self.scan_on else [],
                "talking": self.tx_tg, "connected_s": round(time.time() - self.connected_at)}


class PocGateway:
    def __init__(self, ctl):
        self.ctl = ctl
        self.sessions: dict[str, PocSession] = {}
        self.emergency: dict[int, dict] = {}
        self._aliases: dict[int, str] = {}
        self._task: asyncio.Task | None = None

    @property
    def hub(self):
        return self.ctl.hub

    def alias(self, unit: int) -> str:
        return self.ctl.unit_alias(unit) if unit else "Unknown"

    def is_talking(self, tg: int, src: int) -> bool:
        return any(s.tx_tg == tg and s.unit_id == src for s in self.sessions.values())

    # ------------------------------------------------------------------ rules
    def _load(self, device_id: int) -> tuple[dict | None, dict | None, list[dict], str]:
        """(device, unit, talkgroups, deny_reason) straight from the database."""
        from .call_controller import get_system_settings
        from .dvm import DvmPlan
        with session_scope() as db:
            dev = db.get(PocDevice, device_id)
            if not dev or dev.revoked:
                return None, None, [], "device_revoked"
            sub = db.get(Subscriber, dev.subscriber_id)
            if not sub:
                return None, None, [], "unknown_unit"
            if not sub.enabled:
                return dev.to_dict(), sub.to_dict(), [], "inhibited"
            if not sub.poc_enabled:
                return dev.to_dict(), sub.to_dict(), [], "poc_disabled"
            if get_system_settings(db).get("call_backend") != "dvm":
                return dev.to_dict(), sub.to_dict(), [], "poc_requires_dvm_backend"
            plan = DvmPlan(db)
            if not plan.ack:
                return dev.to_dict(), sub.to_dict(), [], "system_not_ready"
            tgs = [{"id": t["id"], "name": t["name"], "priority": t["priority"],
                    "emergency": t["emergency_enabled"], "mode": t.get("bridge_mode")}
                   for t in plan.poc_talkgroups(sub.to_dict())]
            return dev.to_dict(), sub.to_dict(), tgs, ""

    def authenticate(self, token: str) -> tuple[dict | None, dict | None, list[dict], str]:
        with session_scope() as db:
            dev = db.query(PocDevice).filter(PocDevice.token_hash == token_hash(token or "")).first()
            if not dev:
                return None, None, [], "invalid_token"
            dev.last_seen = now()
            dev_id = dev.id
        return self._load(dev_id)

    def _profile(self, s: PocSession, kind: str) -> dict:
        with session_scope() as db:
            from .call_controller import get_system_settings
            sysset = get_system_settings(db)
        return {"t": kind, "proto": PROTO, "unit": {"id": s.unit_id, "alias": s.unit.get("alias")},
                "system": {"name": sysset.get("system_name", "KeyUp"), "max_call_s": int(sysset.get("max_call_s", 120))},
                "talkgroups": list(s.talkgroups.values()), "selected": s.selected, "scan": s.scan, "scan_on": s.scan_on,
                "network": self._network_ok(), "server_time": time.time()}

    def _network_ok(self) -> bool:
        return bool(self.ctl.fne is not None and self.ctl.fne.reachable)

    async def _resubscribe(self, s: PocSession) -> None:
        want = set()
        if s.selected in s.talkgroups:
            want.add(s.selected)
        if s.scan_on:
            want |= {t for t in s.scan if t in s.talkgroups}
        for tg in s.subscribed - want:
            self.hub.unsubscribe(tg, s.key)
        for tg in want - s.subscribed:
            await self.hub.subscribe(tg, s)
        s.subscribed = want

    # ------------------------------------------------------------------ websocket
    async def handle(self, ws) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except Exception:
            await ws.close(code=4000)
            return
        dev, unit, tgs, reason = self.authenticate(first.get("token", "")) if first.get("t") == "auth" else (None, None, [], "expected_auth")
        if reason:
            await ws.send_text(json.dumps({"t": "auth_fail", "reason": reason}))
            await ws.close(code=4003)
            log.info("PoC auth refused: %s", reason)
            return
        s = PocSession(self, ws, dev, unit)
        s.talkgroups = {t["id"]: t for t in tgs}
        s.selected = next(iter(s.talkgroups), None)
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
        s.closed = True
        if s.tx_tg is not None:
            self.hub.stop_tx(s.tx_tg, s.key)
        self.hub.unsubscribe_all(s.key)
        self.sessions.pop(s.key, None)
        bus.publish("poc", action="offline", unit=s.unit_id, alias=s.unit.get("alias"))

    def _on_audio(self, s: PocSession, data: bytes) -> None:
        if not data or data[0] != AUDIO_UP or s.tx_tg is None:
            return
        self.hub.push(s.tx_tg, s.key, audio.ulaw_to_pcm(data[1:]))

    async def _on_message(self, s: PocSession, m: dict) -> None:
        t = m.get("t")
        if t == "ping":
            await s.send({"t": "pong", "ts": m.get("ts"), "network": self._network_ok()})
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
                await self._dekey(s, "released")
        elif t == "emergency":
            await self._emergency(s, int(m.get("tg") or s.selected or 0), bool(m.get("on", True)))

    async def _key(self, s: PocSession, tg: int, emergency: bool) -> None:
        reason = None
        if tg not in s.talkgroups:
            reason = "not_permitted"
        elif not self._network_ok():
            reason = "network_down"
        elif emergency and not s.talkgroups[tg].get("emergency"):
            emergency = False
        if not reason:
            reason = await self.hub.start_tx(tg, s.key, s.unit_id, emergency)
        if reason:
            await s.send({"t": "deny", "tg": tg, "reason": reason})
            bus.publish("call_denied", tg=tg, src=s.unit_id, site=0, reason=f"poc:{reason}")
            return
        s.tx_tg, s.tx_started, s.tx_emergency = tg, time.time(), emergency
        s.rx_tg = None
        self._seen(s.unit_id, tg)
        await s.send({"t": "grant", "tg": tg, "emergency": emergency})

    async def _dekey(self, s: PocSession, reason: str) -> None:
        if s.tx_tg is None:
            return
        tg, s.tx_tg = s.tx_tg, None
        self.hub.stop_tx(tg, s.key)
        await s.send({"t": "tx_end", "tg": tg, "reason": reason})

    async def _emergency(self, s: PocSession, tg: int, on: bool) -> None:
        if tg not in s.talkgroups or not s.talkgroups[tg].get("emergency"):
            return
        self.emergency[tg] = {"on": on, "src": s.unit_id, "ts": time.time()}
        evt = {"t": "emergency", "tg": tg, "src": s.unit_id, "alias": s.unit.get("alias"), "on": on}
        for other in list(self.sessions.values()):
            if tg in other.talkgroups:
                await other.send(evt)
        bus.publish("emergency", tg=tg, unit=s.unit_id, alias=s.unit.get("alias"), on=on, via="poc")
        log.warning("EMERGENCY %s on TG %s by unit %s (%s)", "declared" if on else "cleared", tg, s.unit_id,
                    s.unit.get("alias"))

    def _seen(self, unit: int, tg: int | None) -> None:
        with session_scope() as db:
            sub = db.get(Subscriber, unit)
            if sub:
                sub.last_seen, sub.last_site_id = now(), 0
                if tg:
                    sub.last_tg_id = tg

    # ------------------------------------------------------------------ housekeeping
    async def refresh(self) -> None:
        """Re-apply rules after an admin change (inhibit, permissions, revocation)."""
        for s in list(self.sessions.values()):
            dev, unit, tgs, reason = self._load(s.device_id)
            if reason:
                await s.send({"t": "inhibited" if reason == "inhibited" else "auth_fail", "reason": reason})
                await self._dekey(s, reason)
                try:
                    await s.ws.close(code=4003)
                except Exception:
                    pass
                await self._close(s)
                continue
            s.unit = unit
            s.talkgroups = {t["id"]: t for t in tgs}
            if s.selected not in s.talkgroups:
                s.selected = next(iter(s.talkgroups), None)
            if s.tx_tg is not None and s.tx_tg not in s.talkgroups:
                await self._dekey(s, "not_permitted")
            await self._resubscribe(s)
            await s.send(self._profile(s, "profile"))

    async def run(self) -> None:
        """Watchdog: transmit timeout, rx_end, network status changes."""
        last_net = None
        while True:
            await asyncio.sleep(0.2)
            t = time.time()
            net = self._network_ok()
            with session_scope() as db:
                from .call_controller import get_system_settings
                max_call = float(get_system_settings(db).get("max_call_s", 120))
            for s in list(self.sessions.values()):
                if s.closed:
                    await self._close(s)
                    continue
                if s.tx_tg is not None and t - s.tx_started > max_call:
                    await self._dekey(s, "timeout")
                if s.rx_tg is not None and t - s.rx_last > RX_IDLE_S:
                    tg, s.rx_tg = s.rx_tg, None
                    await s.send({"t": "rx_end", "tg": tg})
                if net != last_net:
                    await s.send({"t": "status", "network": net})
            last_net = net

    def status(self) -> dict:
        return {"sessions": [s.info() for s in self.sessions.values()],
                "emergencies": {tg: e for tg, e in self.emergency.items() if e.get("on")}}
