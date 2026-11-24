"""Central call processing: edge sessions, floor control, routing and the UDP voice router.

Endpoints are anything that can source or sink a talkgroup call:
  * EdgeSession -- one connected edge device (a site and its base radios)
  * SipLeg      -- an Asterisk channel bridged to a talkgroup (see sip_gateway.py)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

from lmrcommon.modes import MODE_CODEC, Codec, SystemMode
from lmrcommon.protocol import (FrameType, Flags, Msg, VoiceFrame, parse_keepalive, verify_keepalive)

from ..config import settings
from ..db import session_scope
from ..models import (CallRecord, Channel, EdgeDevice, Site, Subscriber, SystemSetting, TalkGroup,
                      Patch, DEFAULT_SYSTEM_SETTINGS, now)
from .rules import Affiliations, link_groups, site_list
from .events import bus
from .transcoder import TranscoderService

log = logging.getLogger("callctl")
EMERGENCY_PRIORITY = 100


def get_system_settings(db) -> dict:
    vals = dict(DEFAULT_SYSTEM_SETTINGS)
    for row in db.query(SystemSetting).all():
        vals[row.key] = row.value
    return vals


# ============================================================== endpoints
class Endpoint:
    key: str
    codec: Codec
    site_id: int = 0
    label: str = ""

    def send_frame(self, frame: VoiceFrame) -> None:
        raise NotImplementedError

    async def call_started(self, call: "Call", channels: list[int]) -> None:
        pass

    async def call_ended(self, call: "Call", reason: str) -> None:
        pass


class EdgeSession(Endpoint):
    def __init__(self, ctl: "CallController", ws, device: EdgeDevice, site: Site, hello: dict, remote_ip: str):
        self.ctl, self.ws = ctl, ws
        self.device_id, self.site_id = device.id, site.id
        self.key = f"site:{site.id}"
        self.label = site.name
        self.remote_ip = remote_ip
        self.voice_key = secrets.token_hex(16)
        self.voice_addr: tuple[str, int] | None = None
        self.vocoders = set(hello.get("vocoders", ["ULAW", "PCM16"]))
        self.mode = "wide_area"
        self.last_heartbeat = time.time()
        self.codec = Codec.ULAW
        self.channels: dict[int, dict] = {}
        self.send_lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(msg))

    def send_frame(self, frame: VoiceFrame) -> None:
        if self.voice_addr and self.ctl.transport:
            self.ctl.transport.sendto(frame.pack(), self.voice_addr)

    async def call_started(self, call, channels):
        await self.send({"type": Msg.CALL_START, "call_id": call.call_id, "tg": call.tg, "src": call.src_id,
                         "src_site": call.src_site, "channels": channels, "codec": self.codec.name,
                         "emergency": call.emergency})

    async def call_ended(self, call, reason):
        await self.send({"type": Msg.CALL_END, "call_id": call.call_id, "tg": call.tg, "reason": reason})


@dataclass
class Call:
    call_id: int
    tg: int
    priority: int
    hang_ms: int
    src_key: str
    src_site: int
    src_channel: int | None
    src_id: int
    codec: Codec
    emergency: bool = False
    via_sip: bool = False
    started: float = field(default_factory=time.time)
    last_voice: float = field(default_factory=time.time)
    state: str = "active"  # active|hang
    hang_until: float = 0.0
    dests: dict[str, list[int]] = field(default_factory=dict)  # endpoint key -> channel ids
    frames: int = 0
    record_id: int | None = None
    seq: int = 0
    linked: frozenset = frozenset()      # talkgroups this call holds the floor on (patches)
    parrot: list | None = None           # recorded (codec, payload) frames to echo back

    def summary(self, ctl: "CallController") -> dict:
        return {
            "call_id": self.call_id, "tg": self.tg, "tg_name": ctl.tg_names.get(self.tg, str(self.tg)),
            "src_id": self.src_id, "src_alias": ctl.unit_alias(self.src_id), "src_site": self.src_site,
            "src_label": ctl.endpoint_label(self.src_key), "codec": self.codec.name, "state": self.state,
            "emergency": self.emergency, "via_sip": self.via_sip, "started": self.started,
            "dests": [ctl.endpoint_label(k) for k in self.dests], "frames": self.frames,
            "linked": sorted(self.linked - {self.tg}), "parrot": self.parrot is not None or self.src_key.startswith("parrot:"),
        }


# ============================================================== controller
class CallController:
    def __init__(self, transcoder: TranscoderService):
        self.transcoder = transcoder
        self.sessions: dict[int, EdgeSession] = {}          # site_id -> session
        self.addr_map: dict[tuple[str, int], EdgeSession] = {}
        self.sip_legs: dict[str, Endpoint] = {}             # key -> leg
        self.calls: dict[int, Call] = {}                    # call_id -> call
        self.tg_calls: dict[int, int] = {}                  # tg -> call_id
        self.transport: asyncio.DatagramTransport | None = None
        self._ids = itertools.count(int(time.time()) & 0xFFFFFF)
        self.tg_names: dict[int, str] = {}
        self.unit_aliases: dict[int, str] = {}
        self.stats = {"calls_granted": 0, "calls_denied": 0, "frames_routed": 0, "frames_dropped": 0}
        self.sip_gateway = None  # set by main
        self.links: dict[int, frozenset[int]] = {}   # tg -> patched talkgroup set
        self.tg_rules: dict[int, dict] = {}
        self.affiliations = Affiliations()
        self.aff_timeout = 1800.0
        self._parrot_ids = itertools.count(1)
        self.fne = None          # FneMonitor when call_backend == dvm
        self.hub = None          # TgAudioHub (SIP + PoC interconnect) when call_backend == dvm
        self.poc = None          # PocGateway when call_backend == dvm

    # -------------------------------------------------------------- helpers
    def endpoint(self, key: str) -> Endpoint | None:
        if key.startswith("site:"):
            return self.sessions.get(int(key[5:]))
        return self.sip_legs.get(key)

    def endpoint_label(self, key: str) -> str:
        ep = self.endpoint(key)
        return ep.label if ep else key

    def unit_alias(self, uid: int) -> str:
        return self.unit_aliases.get(uid, str(uid) if uid else "unknown")

    def refresh_names(self) -> None:
        with session_scope() as db:
            tgs = db.query(TalkGroup).all()
            self.tg_names = {t.id: t.name for t in tgs}
            self.tg_rules = {t.id: {"site_ids": site_list(t.site_ids), "affiliated_only": t.affiliated_only,
                                    "parrot": t.parrot, "restricted": t.restricted} for t in tgs}
            self.unit_aliases = {s.id: s.alias for s in db.query(Subscriber).all()}
            self.links = link_groups([p.to_dict() for p in db.query(Patch).all()])
            self.aff_timeout = float(get_system_settings(db).get("affiliation_timeout_s", 1800))

    def linked(self, tg: int) -> frozenset[int]:
        return self.links.get(tg, frozenset({tg}))

    def floor_call(self, tg: int) -> "Call | None":
        for t in self.linked(tg):
            call = self.calls.get(self.tg_calls.get(t, -1))
            if call:
                return call
        return None

    def site_codec(self, site: Site, vocoders: set[str]) -> Codec:
        if site.network_codec and site.network_codec != "AUTO":
            return Codec[site.network_codec]
        modes = {c.mode for c in site.channels if c.enabled}
        codecs = {MODE_CODEC[SystemMode(m)] for m in modes}
        if len(codecs) == 1:
            c = codecs.pop()
            if c.name in vocoders:
                return c
        return Codec.ULAW

    # ---------------------------------------------------------- config push
    def build_config(self, db, site_id: int) -> dict:
        site = db.get(Site, site_id)
        sysset = get_system_settings(db)
        channels = [c.to_dict() for c in site.channels]
        tg_ids = {c["talkgroup_id"] for c in channels if c["talkgroup_id"]}
        tgs = [t.to_dict() for t in db.query(TalkGroup).all()]
        subs = []
        for s in db.query(Subscriber).filter(Subscriber.enabled.is_(True)).all():
            ids = {t.id for t in s.talkgroups}
            if s.home_site_id == site_id or not ids or ids & tg_ids:
                subs.append({"id": s.id, "alias": s.alias, "home": s.home_site_id == site_id,
                             "talkgroups": sorted(ids), "notify": s.notify_site_trunking})
        cfg = {"type": Msg.CONFIG, "site": site.to_dict(), "channels": channels, "talkgroups": tgs,
               "subscribers": subs, "settings": sysset, "backend": sysset.get("call_backend", "dvm")}
        if cfg["backend"] == "dvm":
            from .dvm import DvmPlan
            plan = DvmPlan(db)
            cfg["dvm"] = {"license_acknowledged": plan.ack,
                          "instances": plan.site_instances(site_id) if plan.ack else {},
                          "warnings": plan.warnings}
        return cfg

    async def push_config(self, site_id: int) -> None:
        sess = self.sessions.get(site_id)
        if not sess:
            return
        with session_scope() as db:
            cfg = self.build_config(db, site_id)
            site = db.get(Site, site_id)
            sess.codec = self.site_codec(site, sess.vocoders)
            sess.channels = {c["id"]: c for c in cfg["channels"]}
        cfg["network_codec"] = sess.codec.name
        try:
            await sess.send(cfg)
        except Exception as e:
            log.warning("config push to site %d failed: %s", site_id, e)

    async def push_all_configs(self) -> None:
        self.refresh_names()
        for sid in list(self.sessions):
            await self.push_config(sid)

    # ------------------------------------------------------- edge sessions
    async def register_edge(self, sess: EdgeSession) -> None:
        old = self.sessions.get(sess.site_id)
        if old:
            await self.unregister_edge(old, reason="replaced")
            try:
                await old.ws.close()
            except Exception:
                pass
        self.sessions[sess.site_id] = sess
        self._set_site_status(sess.site_id, "online", connected=True, device_id=sess.device_id)
        await sess.send({"type": Msg.HELLO_ACK, "site_id": sess.site_id, "voice_key": sess.voice_key,
                         "voice_port": settings.voice_port, "server_time": time.time()})
        await self.push_config(sess.site_id)
        bus.publish("site", site_id=sess.site_id, status="online")
        log.info("edge site %d (%s) connected from %s", sess.site_id, sess.label, sess.remote_ip)

    async def unregister_edge(self, sess: EdgeSession, reason: str = "disconnected") -> None:
        if self.sessions.get(sess.site_id) is not sess:
            return
        del self.sessions[sess.site_id]
        for addr, s in list(self.addr_map.items()):
            if s is sess:
                del self.addr_map[addr]
        # end calls sourced here, drop it from others
        for call in list(self.calls.values()):
            if call.src_key == sess.key:
                await self.end_call(call.call_id, f"source_{reason}")
            else:
                call.dests.pop(sess.key, None)
        self._set_site_status(sess.site_id, "offline", connected=False, device_id=sess.device_id)
        bus.publish("site", site_id=sess.site_id, status="offline")
        log.info("edge site %d %s", sess.site_id, reason)

    def _set_site_status(self, site_id: int, status: str, connected: bool | None = None,
                         device_id: int | None = None, telemetry: dict | None = None) -> None:
        with session_scope() as db:
            site = db.get(Site, site_id)
            if site:
                site.status = status
                site.last_seen = now()
            if device_id:
                dev = db.get(EdgeDevice, device_id)
                if dev:
                    if connected is not None:
                        dev.connected = connected
                    dev.last_seen = now()
                    if telemetry is not None:
                        dev.telemetry = json.dumps(telemetry)
                        dev.agent_version = telemetry.get("version", dev.agent_version)

    async def handle_edge_message(self, sess: EdgeSession, msg: dict) -> None:
        t = msg.get("type")
        if t == Msg.HEARTBEAT:
            sess.last_heartbeat = time.time()
            mode = msg.get("mode", "wide_area")
            if mode != sess.mode:
                bus.publish("site", site_id=sess.site_id, status=mode if mode == "site_trunking" else "online",
                            detail={"mode": mode})
            sess.mode = mode
            self._set_site_status(sess.site_id, "site_trunking" if mode == "site_trunking" else "online",
                                  device_id=sess.device_id, telemetry=msg.get("telemetry", {}))
            await sess.send({"type": Msg.HEARTBEAT_ACK, "ts": time.time()})
        elif t == Msg.CALL_REQUEST:
            await self.request_call(sess, msg)
        elif t == Msg.CALL_END:
            await self.source_ended(msg.get("call_id", 0), sess.key)
        elif t == Msg.AFFILIATE:
            self._seen_unit(msg.get("unit", 0), sess.site_id, msg.get("tg"))
        elif t == Msg.CALL_LOG_UPLOAD:
            self._store_site_trunking_logs(sess.site_id, msg.get("calls", []))
        elif t == Msg.SITE_STATUS:
            bus.publish("site", site_id=sess.site_id, status="online", detail=msg)

    def _seen_unit(self, unit: int, site_id: int, tg: int | None) -> None:
        if not unit:
            return
        with session_scope() as db:
            s = db.get(Subscriber, unit)
            if s:
                s.last_site_id, s.last_seen = site_id, now()
                if tg:
                    s.last_tg_id = tg

    def _store_site_trunking_logs(self, site_id: int, calls: list[dict]) -> None:
        with session_scope() as db:
            for c in calls:
                start = dt.datetime.utcfromtimestamp(c.get("started", time.time()))
                db.add(CallRecord(call_id=0, talkgroup_id=c.get("tg", 0), source_id=c.get("src", 0),
                                  source_site_id=site_id, source_channel_id=c.get("channel"),
                                  started_at=start, ended_at=start + dt.timedelta(seconds=c.get("duration", 0)),
                                  duration_s=c.get("duration", 0), site_trunking=True,
                                  dest_sites=json.dumps([f"site:{site_id}"]), result=c.get("result", "granted")))
        bus.publish("calls_imported", site_id=site_id, count=len(calls))
        log.info("imported %d site-trunking call records from site %d", len(calls), site_id)

    # ------------------------------------------------------------ floor control
    def _authorize(self, db, tg: TalkGroup | None, unit: int, sysset: dict, site_id: int = 0) -> str | None:
        if not tg or not tg.enabled:
            return "unknown_talkgroup"
        if site_id and site_list(tg.site_ids) and site_id not in site_list(tg.site_ids):
            return "site_not_permitted"
        if tg.restricted:
            sub = db.get(Subscriber, unit) if unit else None
            if not sub or tg.id not in {t.id for t in sub.talkgroups}:
                return "not_permitted"
        if unit:
            sub = db.get(Subscriber, unit)
            if not sub:
                if sysset.get("require_known_subscriber") == "true":
                    return "unknown_unit"
            else:
                if not sub.enabled:
                    return "unit_disabled"
                allowed = {t.id for t in sub.talkgroups}
                if allowed and tg.id not in allowed:
                    return "not_permitted"
        elif sysset.get("require_known_subscriber") == "true":
            return "unknown_unit"
        return None

    def _site_allowed(self, tg: int, site_id: int, src_site: int) -> bool:
        rule = self.tg_rules.get(tg, {})
        if rule.get("site_ids") and site_id not in rule["site_ids"]:
            return False
        if rule.get("affiliated_only") and site_id != src_site:
            return site_id in self.affiliations.sites_for(tg, self.aff_timeout)
        return True

    def _destinations(self, db, tg_id: int, exclude_key: str, exclude_channel: int | None,
                      src_site: int = 0) -> dict[str, list[int]]:
        if self.tg_rules.get(tg_id, {}).get("parrot"):
            return {}  # parrot: nobody hears it live; it is echoed back to the talker
        group = self.linked(tg_id)
        dests: dict[str, list[int]] = {}
        for sid, sess in self.sessions.items():
            if sess.mode == "site_trunking":
                continue
            chans = [cid for cid, c in sess.channels.items()
                     if c.get("talkgroup_id") in group and c.get("enabled") and c.get("role", "voice") == "voice"
                     and self._site_allowed(c["talkgroup_id"], sid, src_site)
                     and not (sess.key == exclude_key and cid == exclude_channel)]
            if chans:
                dests[sess.key] = chans
        for key, leg in self.sip_legs.items():
            if getattr(leg, "tg", None) in group and key != exclude_key:
                dests[key] = []
        return dests

    async def request_call(self, src: Endpoint, msg: dict) -> dict:
        """Floor request from an edge (COR) or SIP leg (DTMF/VOX). Returns grant/deny message."""
        tg_id = int(msg.get("tg") or 0)
        channel = msg.get("channel")
        unit = int(msg.get("src") or 0)
        emergency = bool(msg.get("emergency"))
        with session_scope() as db:
            sysset = get_system_settings(db)
            tg = db.get(TalkGroup, tg_id)
            reason = self._authorize(db, tg, unit, sysset, src.site_id)
            prio = (EMERGENCY_PRIORITY if emergency and tg and tg.emergency_enabled else (tg.priority if tg else 0))
            hang = tg.hang_time_ms if tg else int(sysset["default_hang_time_ms"])

            existing = self.floor_call(tg_id)
            if not reason and existing and existing.state == "active" and existing.src_key != src.key:
                if prio > existing.priority:
                    await self._preempt(existing)
                else:
                    reason = "busy"
            if reason:
                self.stats["calls_denied"] += 1
                db.add(CallRecord(call_id=0, talkgroup_id=tg_id, source_id=unit, source_site_id=src.site_id,
                                  source_channel_id=channel, result=f"denied:{reason}", ended_at=now()))
                deny = {"type": Msg.CALL_DENY, "tg": tg_id, "channel": channel, "reason": reason}
                bus.publish("call_denied", tg=tg_id, src=unit, site=src.site_id, reason=reason)
                if isinstance(src, EdgeSession):
                    await src.send(deny)
                return deny

            self.affiliations.heard(unit, src.site_id, tg_id)
            dests = self._destinations(db, tg_id, src.key, channel, src.site_id)
            existing = self.floor_call(tg_id)
            if existing:  # reuse during hang time (or same source re-key): keep call id, update floor
                call = existing
                old_dests = dict(call.dests)
                call.src_key, call.src_site, call.src_channel, call.src_id = src.key, src.site_id, channel, unit
                call.codec, call.state, call.last_voice = src.codec, "active", time.time()
                call.tg = tg_id  # a reply may come in on the other side of a patch
                call.emergency, call.priority = call.emergency or emergency, max(call.priority, prio)
                call.dests = dests
                self.transcoder.close(call.call_id)
                for key in set(old_dests) - set(dests):
                    ep = self.endpoint(key)
                    if ep:
                        await ep.call_started(call, [])
            else:
                call = Call(call_id=next(self._ids) & 0xFFFFFFFF, tg=tg_id, priority=prio, hang_ms=hang,
                            src_key=src.key, src_site=src.site_id, src_channel=channel, src_id=unit,
                            codec=src.codec, emergency=emergency, via_sip=src.key.startswith("sip:"), dests=dests,
                            linked=self.linked(tg_id))
                if self.tg_rules.get(tg_id, {}).get("parrot"):
                    call.parrot = []
                rec = CallRecord(call_id=call.call_id, talkgroup_id=tg_id, source_id=unit,
                                 source_site_id=src.site_id, source_channel_id=channel,
                                 source_codec=src.codec.name, emergency=call.emergency, via_sip=call.via_sip,
                                 dest_sites=json.dumps(list(dests)))
                db.add(rec)
                db.flush()
                call.record_id = rec.id
                self.calls[call.call_id] = call
                for t in call.linked:
                    self.tg_calls[t] = call.call_id
        self._seen_unit(unit, src.site_id, tg_id)
        self.stats["calls_granted"] += 1

        grant = {"type": Msg.CALL_GRANT, "call_id": call.call_id, "tg": tg_id, "channel": channel,
                 "codec": src.codec.name, "dests": len(call.dests)}
        if isinstance(src, EdgeSession):
            await src.send(grant)
        for key, chans in call.dests.items():
            ep = self.endpoint(key)
            if ep:
                await ep.call_started(call, chans)
        bus.publish("call_start", call=call.summary(self))
        return grant

    async def _preempt(self, call: Call) -> None:
        ep = self.endpoint(call.src_key)
        if isinstance(ep, EdgeSession):
            await ep.send({"type": Msg.CALL_DENY, "tg": call.tg, "channel": call.src_channel,
                           "call_id": call.call_id, "reason": "preempted"})
        await self.end_call(call.call_id, "preempted")

    async def source_ended(self, call_id: int, src_key: str) -> None:
        call = self.calls.get(call_id)
        if not call or call.src_key != src_key or call.state != "active":
            return
        if call.parrot is not None:
            recording, src, channel = call.parrot, call.src_key, call.src_channel
            await self.end_call(call.call_id, "parrot_recorded")
            asyncio.get_running_loop().create_task(self._parrot_playback(call, recording, src, channel))
            return
        call.state = "hang"
        call.hang_until = time.time() + call.hang_ms / 1000
        bus.publish("call_hang", call=call.summary(self))

    async def end_call(self, call_id: int, reason: str = "ended") -> None:
        call = self.calls.pop(call_id, None)
        if not call:
            return
        for t in call.linked | {call.tg}:
            if self.tg_calls.get(t) == call_id:
                del self.tg_calls[t]
        transcoded = self.transcoder.close(call_id)
        for key in call.dests:
            ep = self.endpoint(key)
            if ep:
                try:
                    await ep.call_ended(call, reason)
                except Exception as e:
                    log.debug("call_end to %s failed: %s", key, e)
        src = self.endpoint(call.src_key)
        if isinstance(src, EdgeSession) and reason not in ("source_disconnected",):
            try:
                await src.call_ended(call, reason)
            except Exception:
                pass
        dur = time.time() - call.started
        with session_scope() as db:
            rec = db.get(CallRecord, call.record_id) if call.record_id else None
            if rec:
                rec.ended_at, rec.duration_s, rec.frames = now(), round(dur, 2), call.frames
                rec.transcoded = transcoded
                rec.dest_sites = json.dumps(list(call.dests))
                if reason == "preempted":
                    rec.result = "preempted"
        bus.publish("call_end", call_id=call_id, tg=call.tg, reason=reason, duration=round(dur, 1))

    async def _parrot_playback(self, orig: Call, recording: list, src_key: str, channel: int | None) -> None:
        """Echo a parrot-talkgroup transmission back to the radio that sent it."""
        with session_scope() as db:
            delay = int(get_system_settings(db).get("parrot_delay_ms", 1000)) / 1000
        await asyncio.sleep(delay)
        ep = self.endpoint(src_key)
        if not ep or not recording or self.floor_call(orig.tg):
            return
        call = Call(call_id=next(self._ids) & 0xFFFFFFFF, tg=orig.tg, priority=orig.priority, hang_ms=0,
                    src_key=f"parrot:{next(self._parrot_ids)}", src_site=0, src_channel=None, src_id=orig.src_id,
                    codec=ep.codec, dests={src_key: [channel] if channel else []}, linked=frozenset({orig.tg}))
        self.calls[call.call_id] = call
        self.tg_calls[orig.tg] = call.call_id
        await ep.call_started(call, [channel] if channel else [])
        bus.publish("call_start", call=call.summary(self))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i, (codec, payload) in enumerate(recording):
            if call.call_id not in self.calls:
                return  # preempted
            if codec == ep.codec:
                ep.send_frame(VoiceFrame(FrameType.VOICE, codec, 0, call.call_id, call.tg, call.src_id,
                                         i & 0xFFFF, 0, payload))
                call.frames += 1
            await asyncio.sleep(max(0.0, t0 + (i + 1) * 0.02 - loop.time()))
        await self.end_call(call.call_id, "parrot_done")

    def network_status(self) -> dict:
        sites = []
        for sid, sess in sorted(self.sessions.items()):
            sites.append({"site_id": sid, "label": sess.label, "mode": sess.mode, "codec": sess.codec.name,
                          "voice_bound": bool(sess.voice_addr), "remote_ip": sess.remote_ip,
                          "heartbeat_age_s": round(time.time() - sess.last_heartbeat, 1),
                          "vocoders": sorted(sess.vocoders)})
        return {"sites": sites, "affiliations": self.affiliations.table(self.aff_timeout),
                "links": {tg: sorted(g) for tg, g in self.links.items()}}

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            t = time.time()
            with session_scope() as db:
                max_call = float(get_system_settings(db)["max_call_s"])
            for call in list(self.calls.values()):
                if call.state == "hang" and t >= call.hang_until:
                    await self.end_call(call.call_id, "hang_expired")
                elif call.state == "active" and t - call.last_voice > 3.0:
                    await self.source_ended(call.call_id, call.src_key)  # lost VOICE_END
                elif t - call.started > max_call and call.state == "active":
                    await self.end_call(call.call_id, "max_call_time")
            for sess in list(self.sessions.values()):
                if t - sess.last_heartbeat > 30:
                    log.warning("site %d heartbeat timeout", sess.site_id)
                    await self.unregister_edge(sess, "heartbeat_timeout")
                    try:
                        await sess.ws.close()
                    except Exception:
                        pass

    # --------------------------------------------------------------- voice
    def route_frame(self, src_key: str, frame: VoiceFrame) -> None:
        call = self.calls.get(frame.call_id)
        if not call or call.src_key != src_key or call.state != "active" and frame.type != FrameType.VOICE_END:
            self.stats["frames_dropped"] += 1
            return
        call.last_voice = time.time()
        call.frames += 1
        call.seq = (call.seq + 1) & 0xFFFF
        if call.parrot is not None and frame.payload and len(call.parrot) < 50 * 120:
            call.parrot.append((frame.codec, frame.payload))
        if frame.type == FrameType.VOICE_END:
            asyncio.get_running_loop().create_task(self.source_ended(call.call_id, src_key))
        targets: dict[Codec, list[Endpoint]] = {}
        for key in call.dests:
            ep = self.endpoint(key)
            if ep:
                targets.setdefault(ep.codec, []).append(ep)
        if not targets:
            return
        flags = frame.flags | (Flags.EMERGENCY if call.emergency else 0)
        if frame.payload:
            ts = self.transcoder.session(call.call_id, frame.codec)
            converted = ts.convert(frame.payload, set(targets))
        else:
            converted = {c: b"" for c in targets}
        for codec, eps in targets.items():
            if codec not in converted:
                continue
            out = VoiceFrame(frame.type, codec, call.src_site, call.call_id, call.tg, call.src_id,
                             call.seq, flags, converted[codec])
            for ep in eps:
                ep.send_frame(out)
                self.stats["frames_routed"] += 1

    def on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            frame = VoiceFrame.unpack(data)
        except ValueError:
            self.stats["frames_dropped"] += 1
            return
        if frame.type == FrameType.KEEPALIVE:
            sess = self.sessions.get(frame.site)
            if not sess:
                return
            try:
                dev_id, ts, body, mac = parse_keepalive(frame)
            except ValueError:
                return
            if dev_id != sess.device_id or not verify_keepalive(body, mac, sess.voice_key, ts):
                log.warning("bad keepalive for site %d from %s", frame.site, addr)
                return
            if not settings.allow_untunneled_edges and addr[0] != sess.remote_ip:
                log.warning("keepalive for site %d from unexpected address %s", frame.site, addr)
                return
            if sess.voice_addr != addr:
                if sess.voice_addr:
                    self.addr_map.pop(sess.voice_addr, None)
                sess.voice_addr = addr
                self.addr_map[addr] = sess
                log.info("site %d voice path bound to %s:%d", sess.site_id, *addr)
            return
        sess = self.addr_map.get(addr)
        if not sess or frame.site != sess.site_id:
            self.stats["frames_dropped"] += 1
            return
        self.route_frame(sess.key, frame)

    def status(self) -> dict:
        calls = [c.summary(self) for c in self.calls.values()]
        if self.fne is not None:
            calls += list(self.fne.active.values())
        return {
            "sites_connected": len(self.sessions),
            "sip_legs": len(self.sip_legs),
            "active_calls": calls,
            **self.stats,
        }


class VoiceProtocol(asyncio.DatagramProtocol):
    def __init__(self, ctl: CallController):
        self.ctl = ctl

    def connection_made(self, transport):
        self.ctl.transport = transport

    def datagram_received(self, data, addr):
        try:
            self.ctl.on_datagram(data, addr[:2])
        except Exception:
            log.exception("voice datagram error")
