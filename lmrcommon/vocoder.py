"""Vocoder abstraction + pool shared by central (transcoding) and edge (vocoding).

Backends:
  * Software:  PCM16 <-> G.711 mu-law (always available)
  * DVSI:      IMBE / AMBE+2 via AMBE-3000R USB devices (see dvsi.py)

A vocoder channel is stateful (it carries inter-frame prediction), so each call
direction leases its own channel from the pool and returns it at call end.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from . import audio
from .modes import Codec

log = logging.getLogger("vocoder")


class VocoderUnavailable(RuntimeError):
    pass


class VocoderChannel:
    codec: Codec
    name = "base"

    def encode(self, pcm: bytes) -> bytes:  # PCM16 -> codec
        raise NotImplementedError

    def decode(self, data: bytes) -> bytes:  # codec -> PCM16
        raise NotImplementedError

    def release(self) -> None:
        pass


class PcmChannel(VocoderChannel):
    name = "software-pcm"

    def __init__(self):
        self.codec = Codec.PCM16

    def encode(self, pcm):
        return pcm

    def decode(self, data):
        return data


class UlawChannel(VocoderChannel):
    name = "software-g711u"

    def __init__(self):
        self.codec = Codec.ULAW

    def encode(self, pcm):
        return audio.pcm_to_ulaw(pcm)

    def decode(self, data):
        return audio.ulaw_to_pcm(data)


class DVSIChannel(VocoderChannel):
    def __init__(self, dev, codec: Codec, pool: "VocoderPool"):
        self.dev, self.codec, self.pool = dev, codec, pool
        self.name = f"dvsi:{dev.port}"
        dev.set_codec(codec)

    def encode(self, pcm):
        return self.dev.encode(pcm)

    def decode(self, data):
        return self.dev.decode(data)

    def release(self):
        self.pool._return(self.dev)


@dataclass
class PoolStats:
    devices: list = field(default_factory=list)
    leased: int = 0
    lease_failures: int = 0


class VocoderPool:
    def __init__(self, dvsi_ports: list[str] | None = None, baud: int = 460800, rates: dict | None = None):
        self._free = []
        self._all = []
        self._lock = threading.Lock()
        self.stats = PoolStats()
        for port in dvsi_ports or []:
            try:
                from .dvsi import DVSIVocoder
                dev = DVSIVocoder(port, baud, rates)
                self._free.append(dev)
                self._all.append(dev)
            except Exception as e:  # pragma: no cover - hardware
                log.error("DVSI device %s unavailable: %s", port, e)
        self.stats.devices = [{"port": d.port, "product": d.product} for d in self._all]

    @property
    def hardware_count(self) -> int:
        return len(self._all)

    def available(self, codec: Codec) -> bool:
        return Codec(codec) in (Codec.PCM16, Codec.ULAW) or bool(self._free)

    def acquire(self, codec: Codec) -> VocoderChannel:
        codec = Codec(codec)
        if codec == Codec.PCM16:
            return PcmChannel()
        if codec == Codec.ULAW:
            return UlawChannel()
        with self._lock:
            if not self._free:
                self.stats.lease_failures += 1
                raise VocoderUnavailable(f"no free DVSI vocoder for {codec.name}")
            dev = self._free.pop()
            self.stats.leased += 1
        try:
            return DVSIChannel(dev, codec, self)
        except Exception:
            self._return(dev)
            raise

    def _return(self, dev) -> None:
        with self._lock:
            if dev not in self._free:
                self._free.append(dev)
                self.stats.leased -= 1

    def status(self) -> dict:
        return {
            "hardware_vocoders": self.stats.devices,
            "free": len(self._free),
            "leased": self.stats.leased,
            "lease_failures": self.stats.lease_failures,
        }
