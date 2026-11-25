"""Talkgroup call recording.

The router hands every frame of a recorded call to the recorder (CallController.route_frame), already
converted to G.711 mu-law by the call's transcoder. Each call becomes one file:

    <data>/recordings/YYYY/MM/DD/<record id>-tg<TG>.ul     raw mu-law, 8 kHz mono

Silence between transmissions inside a call (hang time, talkback) is kept in real time, up to
MAX_GAP_S, so the file sounds like the channel did. Who talked when is logged per call
(CallRecord.talkers). Playback and download convert to a standard PCM WAV on the fly.

A digital call (IMBE / AMBE+2) that cannot be converted because no DVSI vocoder is free is stored as
its raw voice frames (".imbe" / ".ambe2") with a note, so nothing is lost; those files play once
converted with a vocoder.

Retention: files older than recording_retention_days are deleted, and the oldest go first when the
folder grows past recording_max_gb. Call records stay (marked "deleted by retention").
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import struct
import time
import wave
from dataclasses import dataclass, field

from lmrcommon import audio
from lmrcommon.modes import Codec

from ..config import settings
from ..db import session_scope
from ..models import CallRecord, TalkGroup, now
from .events import bus

log = logging.getLogger("recorder")
FRAME = 160          # mu-law bytes per 20 ms
MAX_GAP_S = 10.0     # longest silence kept between transmissions
TALKER_GAP_S = 0.5   # a pause this long starts a new talker entry
RAW_EXT = {Codec.IMBE: "imbe", Codec.AMBE2: "ambe2"}
ULAW_SILENCE = b"\xff" * FRAME


def recordings_dir() -> str:
    return os.path.join(settings.data_dir, "recordings")


@dataclass
class Take:
    record_id: int
    tg: int
    path: str
    fh: object
    started: float
    codec: str = "ULAW"
    bytes: int = 0
    last_frame: float = 0.0
    talkers: list = field(default_factory=list)
    note: str = ""


class Recorder:
    def __init__(self, ctl):
        self.ctl = ctl
        self.takes: dict[int, Take] = {}       # call_id -> open recording
        self.enabled = True
        self.tg_record: dict[int, bool] = {}
        self.stats = {"recorded_calls": 0, "bytes_written": 0, "deleted_by_retention": 0}
        self.shipper = None                    # set by main (services/shipping.py)

    def refresh(self, sysset: dict) -> None:
        self.enabled = sysset.get("recording_enabled", "true") == "true"
        with session_scope() as db:
            self.tg_record = {t.id: t.record for t in db.query(TalkGroup).all()}

    def wants(self, call) -> bool:
        return self.enabled and call.record_id is not None and self.tg_record.get(call.tg, True) \
            and call.parrot is None and not call.src_key.startswith("parrot:")

    # ------------------------------------------------------------ router hooks
    def frame(self, call, frame, ulaw: bytes | None) -> None:
        """One routed frame. `ulaw` is the frame as mu-law, or None if it could not be converted."""
        if not self.wants(call) or not frame.payload:
            return
        take = self.takes.get(call.call_id)
        t = time.time()
        if take is None:
            take = self._open(call, Codec.ULAW if ulaw is not None else Codec(frame.codec))
            if take is None:
                return
        raw = take.codec != "ULAW"
        if raw and ulaw is not None:
            ulaw = None       # stay consistent within one file
        # talker log
        cur = take.talkers[-1] if take.talkers else None
        if cur is None or cur["src"] != frame.src or t - take.last_frame > TALKER_GAP_S:
            take.talkers.append({"src": frame.src, "alias": self.ctl.unit_alias(frame.src),
                                 "at": max(0.0, round(t - take.started, 2)), "dur": 0.0,
                                 "via": self.ctl.endpoint_label(call.src_key)})
        else:
            cur["dur"] = round(t - take.started - cur["at"], 2)
        # real-time silence between transmissions (mu-law only)
        if not raw and take.last_frame and t - take.last_frame > 0.1:
            gap_frames = int(min(t - take.last_frame, MAX_GAP_S) / 0.02) - 1
            if gap_frames > 0:
                self._write(take, ULAW_SILENCE * gap_frames)
        take.last_frame = t
        if raw:
            self._write(take, struct.pack(">H", len(frame.payload)) + frame.payload)
        else:
            self._write(take, ulaw)

    def call_ended(self, call) -> None:
        take = self.takes.pop(call.call_id, None)
        if take is None:
            return
        try:
            take.fh.close()
        except Exception:
            pass
        if take.talkers:
            last = take.talkers[-1]
            last["dur"] = round(max(last["dur"], take.last_frame - take.started - last["at"]), 2)
        rel = os.path.relpath(take.path, recordings_dir())
        with session_scope() as db:
            rec = db.get(CallRecord, take.record_id)
            if rec:
                rec.recording, rec.recording_bytes = rel, take.bytes
                rec.recording_note = take.note
                rec.talkers = json.dumps(take.talkers)
                if self.shipper and self.shipper.enabled():
                    rec.ship_status = "queued"
        self.stats["recorded_calls"] += 1
        bus.publish("recording", record_id=take.record_id, tg=take.tg, bytes=take.bytes)
        if self.shipper:
            self.shipper.wake()

    def _open(self, call, codec: Codec) -> Take | None:
        day = dt.datetime.utcnow()
        folder = os.path.join(recordings_dir(), day.strftime("%Y"), day.strftime("%m"), day.strftime("%d"))
        ext = "ul" if codec == Codec.ULAW else RAW_EXT.get(codec, "bin")
        path = os.path.join(folder, f"{call.record_id}-tg{call.tg}.{ext}")
        try:
            os.makedirs(folder, exist_ok=True)
            fh = open(path, "wb", buffering=64 * 1024)
        except OSError as e:
            log.error("cannot record call %s: %s", call.call_id, e)
            return None
        note = "" if codec == Codec.ULAW else f"no vocoder free: stored raw {codec.name} voice frames"
        take = Take(call.record_id, call.tg, path, fh, time.time(), codec.name, note=note)
        self.takes[call.call_id] = take
        return take

    def _write(self, take: Take, data: bytes) -> None:
        try:
            take.fh.write(data)
            take.bytes += len(data)
            self.stats["bytes_written"] += len(data)
        except OSError as e:
            log.error("recording write failed: %s", e)

    # ------------------------------------------------------------ files
    @staticmethod
    def path_of(rec: CallRecord) -> str | None:
        if not rec.recording:
            return None
        root = os.path.realpath(recordings_dir())
        p = os.path.realpath(os.path.join(root, rec.recording))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return None
        return p

    @staticmethod
    def wav_bytes(path: str) -> bytes:
        """mu-law recording -> 16-bit PCM WAV (plays in every browser and media player)."""
        with open(path, "rb") as f:
            ulaw = f.read()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(audio.ulaw_to_pcm(ulaw))
        return buf.getvalue()

    # ------------------------------------------------------------ retention
    def enforce_retention(self, days: float, max_gb: float) -> int:
        root = recordings_dir()
        if not os.path.isdir(root):
            return 0
        files = []
        for dirpath, _, names in os.walk(root):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
        files.sort()
        cutoff = time.time() - days * 86400
        total = sum(f[1] for f in files)
        limit = max_gb * 1024 ** 3
        doomed = []
        for mtime, size, p in files:
            if mtime < cutoff or total > limit:
                doomed.append(p)
                total -= size
        open_paths = {t.path for t in self.takes.values()}
        removed = 0
        rels = []
        for p in doomed:
            if p in open_paths:
                continue
            try:
                os.remove(p)
                removed += 1
                rels.append(os.path.relpath(p, root))
            except OSError:
                pass
        if rels:
            with session_scope() as db:
                for i in range(0, len(rels), 500):
                    for rec in db.query(CallRecord).filter(CallRecord.recording.in_(rels[i:i + 500])):
                        rec.recording, rec.recording_note = "", "deleted by retention"
            self.stats["deleted_by_retention"] += removed
            log.info("recording retention removed %d files", removed)
        for dirpath, dirs, names in os.walk(root, topdown=False):
            if dirpath != root and not dirs and not names:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        return removed

    async def housekeeping(self) -> None:
        from .call_controller import get_system_settings
        while True:
            try:
                with session_scope() as db:
                    sysset = get_system_settings(db)
                self.refresh(sysset)
                await asyncio.to_thread(self.enforce_retention, float(sysset.get("recording_retention_days", 30)),
                                        float(sysset.get("recording_max_gb", 20)))
            except Exception:
                log.exception("recording housekeeping failed")
            await asyncio.sleep(600)

    def usage(self) -> dict:
        root = recordings_dir()
        n = size = 0
        for dirpath, _, names in os.walk(root):
            for name in names:
                try:
                    size += os.path.getsize(os.path.join(dirpath, name))
                    n += 1
                except OSError:
                    pass
        return {"files": n, "bytes": size, "recording_now": len(self.takes), "enabled": self.enabled, **self.stats}
