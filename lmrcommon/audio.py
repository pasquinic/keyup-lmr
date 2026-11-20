"""Pure-Python audio helpers: G.711 mu-law, tone generation, levels.

(The stdlib `audioop` module was removed in Python 3.13, so this is self-contained.)
"""
from __future__ import annotations

import array
import math
import sys

SAMPLE_RATE = 8000
_BIAS = 0x84
_CLIP = 32635


def _lin2ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw2lin(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


_ENC = bytes(_lin2ulaw(s - 32768) for s in range(65536))  # index = sample + 32768
_DEC = array.array("h", (_ulaw2lin(u) for u in range(256)))


def pcm_to_samples(pcm: bytes) -> array.array:
    a = array.array("h")
    a.frombytes(pcm)
    if sys.byteorder == "big":
        a.byteswap()
    return a


def samples_to_pcm(samples) -> bytes:
    a = array.array("h", samples)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def pcm_to_ulaw(pcm: bytes) -> bytes:
    return bytes(_ENC[s + 32768] for s in pcm_to_samples(pcm))


def ulaw_to_pcm(data: bytes) -> bytes:
    return samples_to_pcm(_DEC[b] for b in data)


def rms(pcm: bytes) -> float:
    s = pcm_to_samples(pcm)
    if not s:
        return 0.0
    return math.sqrt(sum(x * x for x in s) / len(s))


def silence(frames: int = 1) -> bytes:
    return b"\x00\x00" * 160 * frames


def tone(freq: float, ms: int, amp: float = 0.3, phase0: int = 0) -> bytes:
    n = SAMPLE_RATE * ms // 1000
    a = int(32767 * amp)
    return samples_to_pcm(int(a * math.sin(2 * math.pi * freq * (i + phase0) / SAMPLE_RATE)) for i in range(n))


def tone_sequence(spec: list[tuple[float, int]], amp: float = 0.3) -> bytes:
    """spec = [(freq_hz or 0 for silence, duration_ms), ...]"""
    out = b""
    for f, ms in spec:
        out += tone(f, ms, amp) if f else b"\x00\x00" * (SAMPLE_RATE * ms // 1000)
    return out


def split_frames(pcm: bytes, frame_bytes: int = 320) -> list[bytes]:
    pad = (-len(pcm)) % frame_bytes
    pcm += b"\x00" * pad
    return [pcm[i:i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]


# Standard-ish alert tones (frequency, ms)
TONES = {
    "talk_permit": [(910, 60), (0, 20), (910, 60)],
    "busy": [(480, 250), (0, 250)] * 3,
    "deny": [(1200, 150), (600, 150)] * 2,
    # Motorola-style "site trunking" indication: alternating low tones
    "site_trunking": [(900, 120), (0, 60), (600, 120), (0, 60), (900, 120), (0, 60), (600, 120)],
    "wide_area_restored": [(600, 100), (0, 40), (900, 100), (0, 40), (1200, 160)],
    "emergency": [(1500, 200), (1000, 200)] * 3,
}
