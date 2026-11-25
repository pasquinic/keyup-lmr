"""Ship call recordings to a second server, after each call ends.

Modes (System Settings > recording_ship_mode):
  * https   POST multipart/form-data: "audio" (PCM WAV, or the raw frame file) + "metadata" (JSON),
            Authorization: Bearer <recording_ship_token>. Any 2xx counts as delivered.
  * sip     an ordinary SIP call to recording_ship_target (e.g. sip:recorder@pbx) that plays the call
            in real time; metadata rides in X-KeyUp-* headers and the caller name.
  * siprec  a SIP recording session (RFC 7866) to a session recording server (SRS): the INVITE body
            is multipart/mixed with the SDP and an rs-metadata document (RFC 7865) naming the
            talkgroup, the talkers and the times; the audio streams in real time.
SIP modes need a mu-law recording (a digital call stored as raw frames is sent only over https).

Queue: CallRecord.ship_status "queued" -> "sent" | "retry:N" -> "failed:<why>" after 3 attempts.
Retry failed ones from Call History. Shipping runs one recording at a time, oldest first.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import uuid
from xml.sax.saxutils import escape, quoteattr

import httpx

from ..db import session_scope
from ..models import CallRecord, TalkGroup, now
from .events import bus
from .sipua import SipCall, SipError, SipTarget

log = logging.getLogger("shipping")
MAX_ATTEMPTS = 3
RETRY_S = 60


def _b64id() -> str:
    return base64.b64encode(uuid.uuid4().bytes).decode()


def _iso(t) -> str:
    return (t.isoformat() + "Z") if isinstance(t, dt.datetime) else str(t or "")


class Shipper:
    def __init__(self, recorder):
        self.recorder = recorder
        self.event = asyncio.Event()
        self.cfg: dict = {}
        self.stats = {"sent": 0, "failed": 0, "last_error": ""}
        self.next_try: dict[int, float] = {}

    def load(self) -> dict:
        from .call_controller import get_system_settings
        with session_scope() as db:
            s = get_system_settings(db)
        self.cfg = {k[len("recording_ship_"):]: v for k, v in s.items() if k.startswith("recording_ship_")}
        self.cfg["system_name"] = s.get("system_name", "KeyUp")
        return self.cfg

    def enabled(self) -> bool:
        cfg = self.cfg or self.load()
        return cfg.get("mode", "off") in ("https", "sip", "siprec") and bool(cfg.get("target"))

    def wake(self) -> None:
        self.event.set()

    # ------------------------------------------------------------ metadata
    @staticmethod
    def metadata(rec: CallRecord, tg_name: str, system: str) -> dict:
        return {"system": system, "record_id": rec.id, "call_id": rec.call_id, "talkgroup": rec.talkgroup_id,
                "talkgroup_name": tg_name, "source_id": rec.source_id, "source_site": rec.source_site_id,
                "started_at": _iso(rec.started_at), "ended_at": _iso(rec.ended_at), "duration_s": rec.duration_s,
                "emergency": rec.emergency, "talkers": json.loads(rec.talkers or "[]"),
                "codec": "PCMU" if rec.recording.endswith(".ul") else rec.recording.rsplit(".", 1)[-1].upper(),
                "note": rec.recording_note}

    @staticmethod
    def rs_metadata(meta: dict) -> str:
        """RFC 7865 recording metadata: one session (the talkgroup call), one stream, a participant per talker."""
        sid, stream = _b64id(), _b64id()
        start = meta["started_at"]
        parts, assoc, streams = [], [], []
        talkers = {}
        for t in meta["talkers"] or [{"src": meta["source_id"], "alias": str(meta["source_id"])}]:
            talkers.setdefault(t["src"], t.get("alias") or str(t["src"]))
        for src, alias in talkers.items():
            pid = _b64id()
            parts.append(f'  <participant participant_id="{pid}">\n'
                         f'    <nameID aor="sip:{src}@keyup.radio"><name>{escape(alias)}</name></nameID>\n'
                         f'  </participant>')
            assoc.append(f'  <participantsessionassoc participant_id="{pid}" session_id="{sid}">\n'
                         f'    <associate-time>{start}</associate-time>\n  </participantsessionassoc>')
            streams.append(f'  <participantstreamassoc participant_id="{pid}">\n    <send>{stream}</send>\n'
                           f'  </participantstreamassoc>')
        ext = (f'    <extensiondata xmlns:k="urn:keyup:lmr:1">\n'
               f'      <k:talkgroup id={quoteattr(str(meta["talkgroup"]))} name={quoteattr(meta["talkgroup_name"])}/>\n'
               f'      <k:call record={quoteattr(str(meta["record_id"]))} emergency={quoteattr(str(meta["emergency"]).lower())}'
               f' duration={quoteattr(str(meta["duration_s"]))} system={quoteattr(meta["system"])}/>\n'
               f'    </extensiondata>\n')
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<recording xmlns="urn:ietf:params:xml:ns:recording:1">\n'
                '  <datamode>complete</datamode>\n'
                f'  <session session_id="{sid}">\n    <start-time>{start}</start-time>\n{ext}  </session>\n'
                + "\n".join(parts) + "\n"
                f'  <stream stream_id="{stream}" session_id="{sid}">\n    <label>1</label>\n  </stream>\n'
                f'  <sessionrecordingassoc session_id="{sid}">\n    <associate-time>{start}</associate-time>\n'
                '  </sessionrecordingassoc>\n'
                + "\n".join(assoc) + "\n" + "\n".join(streams) + "\n</recording>\n")

    # ------------------------------------------------------------ delivery
    async def ship(self, rec: CallRecord, tg_name: str) -> None:
        cfg = self.cfg
        path = self.recorder.path_of(rec)
        if not path:
            raise RuntimeError("recording file missing")
        meta = self.metadata(rec, tg_name, cfg.get("system_name", "KeyUp"))
        mode = cfg.get("mode")
        if mode == "https":
            await self._https(path, meta)
            return
        if not path.endswith(".ul"):
            raise RuntimeError("no audio to stream (raw digital frames; use https shipping)")
        with open(path, "rb") as f:
            ulaw = f.read()
        await self._sip(ulaw, meta, siprec=(mode == "siprec"))

    async def _https(self, path: str, meta: dict) -> None:
        if path.endswith(".ul"):
            body, name, ctype = await asyncio.to_thread(self.recorder.wav_bytes, path), f"call-{meta['record_id']}.wav", "audio/wav"
        else:
            with open(path, "rb") as f:
                body = f.read()
            name, ctype = os.path.basename(path), "application/octet-stream"
        headers = {"Authorization": f"Bearer {self.cfg['token']}"} if self.cfg.get("token") else {}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.cfg["target"], headers=headers, data={"metadata": json.dumps(meta)},
                                  files={"audio": (name, body, ctype)})
        if r.status_code // 100 != 2:
            raise RuntimeError(f"HTTP {r.status_code}")

    async def _sip(self, ulaw: bytes, meta: dict, siprec: bool) -> None:
        target = SipTarget.parse(self.cfg["target"], self.cfg.get("transport", "udp"))
        name = f"TG {meta['talkgroup']} {meta['talkgroup_name']}"[:48]
        x = {"X-KeyUp-Talkgroup": str(meta["talkgroup"]), "X-KeyUp-Talkgroup-Name": meta["talkgroup_name"],
             "X-KeyUp-Source": str(meta["source_id"]), "X-KeyUp-Record": str(meta["record_id"]),
             "X-KeyUp-Started": meta["started_at"], "X-KeyUp-Emergency": str(meta["emergency"]).lower()}
        call = SipCall(target, f"tg{meta['talkgroup']}", name, self.cfg.get("username", ""),
                       self.cfg.get("password", ""), x, contact_params=";+sip.src" if siprec else "")
        try:
            if siprec:
                boundary = "keyup-" + uuid.uuid4().hex[:12]
                xml = self.rs_metadata(meta)

                def body(c: SipCall) -> bytes:
                    return (f"--{boundary}\r\nContent-Type: application/sdp\r\n\r\n{c.sdp_offer(label='1')}"
                            f"--{boundary}\r\nContent-Type: application/rs-metadata+xml\r\n"
                            f"Content-Disposition: recording-session\r\n\r\n{xml}"
                            f"--{boundary}--\r\n").encode()
                await call.start(body, f"multipart/mixed;boundary={boundary}",
                                 {"Require": "siprec", "Accept": "application/sdp, application/rs-metadata+xml"})
            else:
                await call.start(lambda c: c.sdp_offer().encode(), "application/sdp")
            if not call.remote_rtp:
                raise SipError("the recorder did not accept any audio stream")
            await call.stream_ulaw(ulaw)
            await call.hangup()
        finally:
            call.close()

    # ------------------------------------------------------------ queue
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self.event.wait(), 30)
            except asyncio.TimeoutError:
                pass
            self.event.clear()
            try:
                self.load()
                if not self.enabled():
                    continue
                while True:
                    with session_scope() as db:
                        q = db.query(CallRecord).filter(CallRecord.recording != "",
                                                        (CallRecord.ship_status == "queued") |
                                                        CallRecord.ship_status.like("retry:%"))
                        rec = next((r for r in q.order_by(CallRecord.id).limit(50)
                                    if self.next_try.get(r.id, 0) <= loop.time()), None)
                        if rec is None:
                            break
                        tg = db.get(TalkGroup, rec.talkgroup_id)
                        db.expunge(rec)
                    await self._attempt(rec, tg.name if tg else str(rec.talkgroup_id))
            except Exception:
                log.exception("shipping loop error")

    async def _attempt(self, rec: CallRecord, tg_name: str) -> None:
        tries = int(rec.ship_status.split(":")[1]) if rec.ship_status.startswith("retry:") else 0
        try:
            await self.ship(rec, tg_name)
            status, shipped = "sent", now()
            self.stats["sent"] += 1
        except Exception as e:
            tries += 1
            why = str(e)[:100] or e.__class__.__name__
            self.stats["last_error"] = why
            status = f"retry:{tries}" if tries < MAX_ATTEMPTS else f"failed:{why}"
            shipped = None
            self.next_try[rec.id] = asyncio.get_running_loop().time() + RETRY_S * tries
            if tries >= MAX_ATTEMPTS:
                self.stats["failed"] += 1
            log.warning("shipping recording %s failed (%d/%d): %s", rec.id, tries, MAX_ATTEMPTS, why)
        with session_scope() as db:
            r = db.get(CallRecord, rec.id)
            if r:
                r.ship_status = status[:160]
                if shipped:
                    r.shipped_at = shipped
        bus.publish("recording_shipped", record_id=rec.id, status=status)

    def status(self) -> dict:
        cfg = self.cfg or self.load()
        return {"mode": cfg.get("mode", "off"), "target": cfg.get("target", ""), **self.stats}
