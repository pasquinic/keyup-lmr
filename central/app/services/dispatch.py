"""Dispatch console gateway: KeyUp Dispatch Console (Windows) sessions.

A console is a router leg like a PoC phone, but it monitors many talkgroups at once and has dispatch
rights. It transmits as its Subscriber's radio ID, so the same talkgroup rules apply as for a radio;
with "call priority" it may talk over radios (not over emergencies).

The console reaches central either through its built-in WireGuard tunnel (ws://<tunnel ip>:8080) or
over HTTPS (wss://<server>), with the same protocol:

  client -> server (JSON)
    {"t":"auth","token","app","machine","transport"}
    {"t":"monitor","tgs":[...]}                          talkgroups whose audio the console wants
    {"t":"ptt","on":true,"tg","emergency"} / {"t":"ptt","on":false}
    {"t":"inhibit","unit","on"}                          (may inhibit) disable / re-enable a radio ID
    {"t":"patch","tgs":[a,b,...],"on":true,"name"?}      (may patch) patch two or more talkgroups together
    {"t":"patch","id":n,"tgs":[c,...],"on":true}         add talkgroups to patch n
    {"t":"patch","id":n,"on":false}                      remove patch n
    {"t":"patch","tgs":[c,...],"on":false}               take these talkgroups out of every patch
    {"t":"emergency_clear","tg"}
    {"t":"ping","ts"}
    binary 0x01 + N*160 bytes G.711 mu-law (20 ms frames)
  server -> client (JSON)
    auth_ok / profile {console, unit, system, zones, talkgroups, rights, patches}
    auth_fail {reason}
    grant {tg} / deny {tg, reason} / tx_end {tg, reason}
    call_start {call_id, tg, src, alias, emergency, from}   call_end {call_id, tg, reason}
    emergency {tg, src, alias, on}   inhibit_result {unit, on, ok, reason}   patches {patches}
    pong {ts}
    binary 0x02 + tg(u32) + src(u32) + call_id(u32) + 160 bytes mu-law
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import time

from lmrcommon.modes import Codec
from lmrcommon.protocol import FrameType, Msg, VoiceFrame

from ..db import session_scope
from ..models import DispatchConsole, Subscriber, TalkgroupPatch, now
from .rules import patch_members
from .call_controller import Endpoint, get_system_settings
from .events import bus
from .poc import token_hash
from .zones import unit_view

log = logging.getLogger("dispatch")
FRAME = 160
CONSOLE_PRIORITY = 50     # above every talkgroup priority (1-10), below emergency (100)
AUDIO_UP, AUDIO_DOWN = 0x01, 0x02


class ConsoleSession(Endpoint):
    def __init__(self, gw: "DispatchGateway", ws, con: dict, unit: dict):
        self.gw, self.ws = gw, ws
        self.console = con
        self.unit = unit
        self.key = f"console:{con['id']}:{secrets.token_hex(3)}"
        self.codec = Codec.ULAW
        self.site_id = 0
        self.label = f"Console {con['name']}"
        self.talkgroups: dict[int, dict] = {}
        self.zones: list[dict] = []
        self.monitored: set[int] = set()
        self.tx_tg: int | None = None
        self.tx_call = 0
        self.tx_seq = 0
        self.tx_emergency = False
        self.transport = "?"
        self.connected_at = time.time()
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.calls_seen: dict[int, int] = {}     # call_id -> tg shown

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

    def _shown_tg(self, tg: int) -> int | None:
        group = self.gw.ctl.linked(tg) | {tg}
        mine = sorted(self.monitored & group)
        if not mine:
            return None
        return tg if tg in mine else mine[0]

    # ------------------------------------------------------------ router endpoint
    def listens(self, group: frozenset) -> bool:
        return bool(self.monitored & group)

    async def call_started(self, call, channels):
        tg = self._shown_tg(call.tg)
        if tg is None:
            return
        self.calls_seen[call.call_id] = tg
        await self.send({"t": "call_start", "call_id": call.call_id, "tg": tg, "call_tg": call.tg, "src": call.src_id,
                         "alias": self.gw.ctl.unit_alias(call.src_id), "emergency": call.emergency,
                         "from": self.gw.ctl.endpoint_label(call.src_key)})

    def send_frame(self, frame: VoiceFrame) -> None:
        if self.closed or not frame.payload or frame.type == FrameType.KEEPALIVE:
            return
        if self.tx_call and frame.call_id == self.tx_call:
            return
        tg = self.calls_seen.get(frame.call_id) or self._shown_tg(frame.tg)
        if tg is None:
            return
        if frame.call_id not in self.calls_seen:
            self.calls_seen[frame.call_id] = tg
        p = frame.payload
        loop = asyncio.get_running_loop()
        for i in range(0, len(p) - FRAME + 1, FRAME):
            loop.create_task(self._send_bytes(struct.pack(">BIII", AUDIO_DOWN, tg, frame.src, frame.call_id) + p[i:i + FRAME]))

    async def call_ended(self, call, reason):
        if call.call_id == self.tx_call and self.tx_tg is not None:
            await self.gw.dekey(self, reason, notify_router=False)
        tg = self.calls_seen.pop(call.call_id, None)
        if tg is not None:
            await self.send({"t": "call_end", "call_id": call.call_id, "tg": tg, "reason": reason})

    async def preempted(self, call):
        if call.call_id == self.tx_call:
            await self.gw.dekey(self, "preempted", notify_router=False)

    def info(self) -> dict:
        return {"key": self.key, "console": self.console["id"], "name": self.console["name"], "unit": self.unit_id,
                "monitoring": sorted(self.monitored), "talking": self.tx_tg, "transport": self.transport,
                "connected_s": round(time.time() - self.connected_at)}


class DispatchGateway:
    def __init__(self, ctl, poc=None):
        self.ctl = ctl
        self.poc = poc
        self.sessions: dict[str, ConsoleSession] = {}

    # ------------------------------------------------------------ rules
    def _load(self, console_id: int) -> tuple[dict | None, dict | None, dict, str]:
        with session_scope() as db:
            con = db.get(DispatchConsole, console_id)
            if not con or not con.token_hash:
                return None, None, {}, "console_revoked"
            if not con.enabled:
                return con.to_dict(), None, {}, "console_disabled"
            sub = db.get(Subscriber, con.subscriber_id)
            if not sub:
                return con.to_dict(), None, {}, "unknown_unit"
            if not sub.enabled:
                return con.to_dict(), sub.to_dict(), {}, "inhibited"
            return con.to_dict(), sub.to_dict(), unit_view(db, sub, zones=list(con.zones)), ""

    def authenticate(self, token: str):
        with session_scope() as db:
            con = db.query(DispatchConsole).filter(DispatchConsole.token_hash == token_hash(token or "-")).first() \
                if token else None
            if not con:
                return None, None, {}, "invalid_token"
            con.last_seen = now()
            cid = con.id
        return self._load(cid)

    def _patches(self) -> list[dict]:
        with session_scope() as db:
            return [{**d, "talkgroups": patch_members(d)} for d in
                    (p.to_dict() for p in db.query(TalkgroupPatch).order_by(TalkgroupPatch.id))]

    def _profile(self, s: ConsoleSession, kind: str) -> dict:
        with session_scope() as db:
            sysset = get_system_settings(db)
        c = s.console
        return {"t": kind, "proto": 1, "console": {"id": c["id"], "name": c["name"]},
                "unit": {"id": s.unit_id, "alias": s.unit.get("alias")},
                "system": {"name": sysset.get("system_name", "KeyUp"), "max_call_s": int(sysset.get("max_call_s", 120))},
                "rights": {"inhibit": c["can_inhibit"], "priority": c["call_priority"], "patch": c["can_patch"]},
                "zones": s.zones, "talkgroups": list(s.talkgroups.values()), "patches": self._patches(),
                "emergencies": {str(tg): e for tg, e in (self.poc.emergency if self.poc else {}).items() if e.get("on")},
                "server_time": time.time()}

    def _apply(self, s: ConsoleSession, con: dict, unit: dict, view: dict) -> None:
        s.console, s.unit = con, unit
        s.talkgroups = {t["id"]: t for t in view.get("talkgroups", [])}
        s.zones = view.get("zones", [])
        s.monitored &= set(s.talkgroups)

    # ------------------------------------------------------------ websocket
    async def handle(self, ws) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except Exception:
            await ws.close(code=4000)
            return
        con, unit, view, reason = self.authenticate(first.get("token", "")) if first.get("t") == "auth" \
            else (None, None, {}, "expected_auth")
        if reason:
            await ws.send_text(json.dumps({"t": "auth_fail", "reason": reason}))
            await ws.close(code=4003)
            log.info("console auth refused: %s", reason)
            return
        s = ConsoleSession(self, ws, con, unit)
        s.transport = str(first.get("transport", "?"))[:16]
        self._apply(s, con, unit, view)
        self.sessions[s.key] = s
        await self.ctl.attach_leg(s)
        await s.send(self._profile(s, "auth_ok"))
        bus.publish("console", action="online", console=con["id"], name=con["name"], transport=s.transport,
                    machine=str(first.get("machine", ""))[:64])
        log.info("console %s (%s) online via %s", con["name"], s.unit_id, s.transport)
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
            log.debug("console session %s ended: %s", s.key, e)
        finally:
            await self._close(s)

    async def _close(self, s: ConsoleSession) -> None:
        if self.sessions.get(s.key) is not s:
            return
        del self.sessions[s.key]
        s.closed = True
        await self.ctl.detach_leg(s)
        bus.publish("console", action="offline", console=s.console["id"], name=s.console["name"])

    def _on_audio(self, s: ConsoleSession, data: bytes) -> None:
        if not data or data[0] != AUDIO_UP or s.tx_tg is None or not s.tx_call:
            return
        body = data[1:]
        for i in range(0, len(body) - FRAME + 1, FRAME):
            s.tx_seq = (s.tx_seq + 1) & 0xFFFF
            self.ctl.route_frame(s.key, VoiceFrame(FrameType.VOICE, Codec.ULAW, 0, s.tx_call, s.tx_tg, s.unit_id,
                                                   s.tx_seq, 1 if s.tx_emergency else 0, body[i:i + FRAME]))

    async def _on_message(self, s: ConsoleSession, m: dict) -> None:
        t = m.get("t")
        if t == "ping":
            await s.send({"t": "pong", "ts": m.get("ts")})
        elif t == "monitor":
            s.monitored = {int(x) for x in m.get("tgs") or [] if int(x) in s.talkgroups}
            await self.ctl.attach_leg(s)
            await s.send({"t": "monitoring", "tgs": sorted(s.monitored)})
        elif t == "ptt":
            if m.get("on"):
                await self._key(s, int(m.get("tg") or 0), bool(m.get("emergency")))
            else:
                await self.dekey(s, "released")
        elif t == "inhibit":
            await self._inhibit(s, int(m.get("unit") or 0), bool(m.get("on", True)))
        elif t == "patch":
            pid = m.get("id")
            await self._patch(s, [int(x) for x in m.get("tgs") or []], bool(m.get("on", True)),
                              int(pid) if pid not in (None, "") else None, str(m.get("name") or "")[:64])
        elif t == "emergency_clear":
            tg = int(m.get("tg") or 0)
            if self.poc and tg in self.poc.emergency:
                self.poc.emergency[tg]["on"] = False
                await self.poc.broadcast_emergency(tg, 0, s.console["name"], False)
            await self.broadcast({"t": "emergency", "tg": tg, "src": 0, "alias": s.console["name"], "on": False})
            bus.publish("emergency", tg=tg, unit=0, alias=s.console["name"], on=False, via="console")

    async def _key(self, s: ConsoleSession, tg: int, emergency: bool) -> None:
        if s.tx_tg is not None:
            return
        if tg not in s.talkgroups:
            await s.send({"t": "deny", "tg": tg, "reason": "not_permitted"})
            return
        req = {"tg": tg, "src": s.unit_id, "emergency": emergency}
        if s.console.get("call_priority"):
            req["priority"] = CONSOLE_PRIORITY
        res = await self.ctl.request_call(s, req)
        if res.get("type") != Msg.CALL_GRANT:
            await s.send({"t": "deny", "tg": tg, "reason": res.get("reason", "denied")})
            return
        s.tx_tg, s.tx_call, s.tx_seq, s.tx_emergency = tg, res["call_id"], 0, emergency
        await s.send({"t": "grant", "tg": tg, "call_id": s.tx_call})

    async def dekey(self, s: ConsoleSession, reason: str, notify_router: bool = True) -> None:
        if s.tx_tg is None:
            return
        tg, call_id = s.tx_tg, s.tx_call
        s.tx_tg, s.tx_call = None, 0
        if notify_router and call_id:
            await self.ctl.source_ended(call_id, s.key)
        await s.send({"t": "tx_end", "tg": tg, "reason": reason})

    async def _inhibit(self, s: ConsoleSession, unit: int, on: bool) -> None:
        if not s.console.get("can_inhibit"):
            await s.send({"t": "inhibit_result", "unit": unit, "on": on, "ok": False, "reason": "not_allowed"})
            return
        with session_scope() as db:
            sub = db.get(Subscriber, unit)
            ok = sub is not None and unit != s.unit_id
            if ok:
                sub.enabled = not on
        await s.send({"t": "inhibit_result", "unit": unit, "on": on, "ok": ok,
                      "reason": "" if ok else ("own_radio_id" if unit == s.unit_id else "unknown_unit")})
        if ok:
            bus.publish("inhibit", unit=unit, on=on, by=s.console["name"])
            log.warning("console %s %s radio %s", s.console["name"], "inhibited" if on else "uninhibited", unit)
            await self._changed()

    async def _patch(self, s: ConsoleSession, tgs: list[int], on: bool, pid: int | None = None,
                     name: str = "") -> None:
        tgs = list(dict.fromkeys(tgs))

        async def fail(reason: str) -> None:
            await s.send({"t": "patch_result", "ok": False, "reason": reason})

        if not s.console.get("can_patch") or any(t not in s.talkgroups for t in tgs):
            return await fail("not_allowed")
        result: dict = {"t": "patch_result", "ok": True, "on": on}
        with session_scope() as db:
            if pid is not None:
                p = db.get(TalkgroupPatch, pid)
                if p is None:
                    return await fail("no_such_patch")
                members = patch_members(p.to_dict())
                if any(t not in s.talkgroups for t in members):
                    return await fail("not_allowed")      # it links talkgroups this console can't see
                if on:
                    members += [t for t in tgs if t not in members]
                    p.talkgroups, p.tg_a, p.tg_b = members, members[0], members[1]
                    result.update(id=p.id, tgs=members)
                else:
                    db.delete(p)
                    result.update(id=pid, tgs=members)
            elif on:
                if len(tgs) < 2:
                    return await fail("need_two")
                for p in db.query(TalkgroupPatch).all():
                    if set(patch_members(p.to_dict())) == set(tgs):
                        p.enabled = True
                        result.update(id=p.id, tgs=tgs)
                        break
                else:
                    p = TalkgroupPatch(name=name or f"Console {s.console['name']}"[:64], talkgroups=tgs,
                                       tg_a=tgs[0], tg_b=tgs[1], enabled=True)
                    db.add(p)
                    db.flush()
                    result.update(id=p.id, tgs=tgs)
            else:
                if not tgs:
                    return await fail("need_talkgroups")
                for p in db.query(TalkgroupPatch).all():
                    members = patch_members(p.to_dict())
                    if not set(tgs) & set(members):
                        continue
                    rest = [t for t in members if t not in tgs]
                    if len(rest) < 2:
                        db.delete(p)
                    else:
                        p.talkgroups, p.tg_a, p.tg_b = rest, rest[0], rest[1]
                result.update(tgs=tgs)
        bus.publish("patch", tgs=result.get("tgs", tgs), on=on, by=s.console["name"])
        await s.send(result)
        await self._changed()

    async def _changed(self) -> None:
        self.ctl.refresh_names()
        await self.ctl.push_all_configs()
        if self.poc:
            await self.poc.refresh()
        await self.refresh()

    async def broadcast(self, msg: dict) -> None:
        for s in list(self.sessions.values()):
            if msg.get("tg") is None or msg["tg"] in s.talkgroups:
                await s.send(msg)

    async def on_event(self, evt: dict) -> None:
        """Bus events consoles care about (emergencies from phones / radios)."""
        if evt.get("event") == "emergency" and evt.get("via") != "console":
            await self.broadcast({"t": "emergency", "tg": evt.get("tg"), "src": evt.get("unit"),
                                  "alias": evt.get("alias"), "on": evt.get("on", True)})

    async def refresh(self) -> None:
        for s in list(self.sessions.values()):
            con, unit, view, reason = self._load(s.console["id"])
            if reason:
                await s.send({"t": "auth_fail", "reason": reason})
                await self.dekey(s, reason)
                try:
                    await s.ws.close(code=4003)
                except Exception:
                    pass
                await self._close(s)
                continue
            self._apply(s, con, unit, view)
            if s.tx_tg is not None and s.tx_tg not in s.talkgroups:
                await self.dekey(s, "not_permitted")
            await self.ctl.attach_leg(s)
            await s.send(self._profile(s, "profile"))

    async def run(self) -> None:
        q = bus.subscribe()
        try:
            while True:
                evt = await q.get()
                try:
                    await self.on_event(evt)
                except Exception:
                    log.exception("console event failed")
        finally:
            bus.unsubscribe(q)

    def status(self) -> dict:
        return {"sessions": [s.info() for s in self.sessions.values()]}
