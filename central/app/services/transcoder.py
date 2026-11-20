"""Multi-mode transcoding for routed calls.

For each active call, a CallTranscoder converts the source codec into every
codec needed by destination sites/SIP legs:

  * same codec               -> passthrough (incl. P25P2 <-> DMR <-> NXDN via AMBE+2)
  * ULAW <-> PCM16           -> software
  * IMBE/AMBE2 involved      -> DVSI hardware decode to PCM16 and/or encode from PCM16

Each source frame is decoded at most once, then encoded once per destination codec.
"""
from __future__ import annotations

import logging
import threading

from lmrcommon.modes import Codec
from lmrcommon.vocoder import VocoderPool, VocoderUnavailable

log = logging.getLogger("transcoder")


class CallTranscoder:
    def __init__(self, pool: VocoderPool, call_id: int, src: Codec):
        self.pool, self.call_id, self.src = pool, call_id, Codec(src)
        self.decoder = None
        self.encoders: dict[Codec, object] = {}
        self.failed: set[Codec] = set()
        self.frames = 0
        self.transcoded = False
        self.lock = threading.Lock()

    def _decoder(self):
        if self.decoder is None:
            self.decoder = self.pool.acquire(self.src)
        return self.decoder

    def _encoder(self, codec: Codec):
        if codec not in self.encoders:
            self.encoders[codec] = self.pool.acquire(codec)
        return self.encoders[codec]

    def convert(self, payload: bytes, targets: set[Codec]) -> dict[Codec, bytes]:
        out: dict[Codec, bytes] = {}
        with self.lock:
            self.frames += 1
            pcm = None
            for dst in targets:
                dst = Codec(dst)
                if dst == self.src:
                    out[dst] = payload
                    continue
                if dst in self.failed:
                    continue
                try:
                    if pcm is None:
                        pcm = self._decoder().decode(payload)
                    out[dst] = self._encoder(dst).encode(pcm)
                    self.transcoded = True
                except VocoderUnavailable as e:
                    log.warning("call %d: cannot transcode %s->%s: %s", self.call_id, self.src.name, dst.name, e)
                    self.failed.add(dst)
                except Exception as e:  # hardware error: drop this frame for this codec
                    log.error("call %d: transcode error %s->%s: %s", self.call_id, self.src.name, dst.name, e)
        return out

    def close(self) -> None:
        with self.lock:
            for ch in [self.decoder, *self.encoders.values()]:
                if ch is not None:
                    ch.release()
            self.decoder = None
            self.encoders.clear()


class TranscoderService:
    def __init__(self, pool: VocoderPool):
        self.pool = pool
        self.sessions: dict[int, CallTranscoder] = {}
        self.total_frames = 0
        self.total_transcoded_calls = 0

    def session(self, call_id: int, src: Codec) -> CallTranscoder:
        s = self.sessions.get(call_id)
        if s is None:
            s = self.sessions[call_id] = CallTranscoder(self.pool, call_id, src)
        return s

    def close(self, call_id: int) -> bool:
        s = self.sessions.pop(call_id, None)
        if not s:
            return False
        self.total_frames += s.frames
        if s.transcoded:
            self.total_transcoded_calls += 1
        s.close()
        return s.transcoded

    def can_bridge(self, src: Codec, dst: Codec) -> bool:
        src, dst = Codec(src), Codec(dst)
        if src == dst:
            return True
        return self.pool.available(src) and self.pool.available(dst)

    def status(self) -> dict:
        return {
            **self.pool.status(),
            "active_sessions": len(self.sessions),
            "total_frames": self.total_frames,
            "transcoded_calls": self.total_transcoded_calls,
            "passthrough_groups": ["P25P2 / DMR / NXDN share AMBE+2 (no tandem vocoding)"],
        }
