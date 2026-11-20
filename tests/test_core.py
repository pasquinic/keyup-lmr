"""Unit tests for shared protocol, audio, DVSI packet framing and transcoding logic.

Run:  PYTHONPATH=.:central pytest -q tests
"""
import struct
import time

import pytest

from lmrcommon import audio
from lmrcommon.dvsi import build_packet, PKT_CONTROL, F_RATET
from lmrcommon.modes import Codec, codec_for_mode
from lmrcommon.protocol import (FrameType, VoiceFrame, make_keepalive, parse_keepalive, verify_keepalive)
from lmrcommon.vocoder import VocoderChannel, VocoderPool, VocoderUnavailable


def test_frame_roundtrip():
    f = VoiceFrame(FrameType.VOICE, Codec.AMBE2, 7, 123456, 100, 1001, 42, 1, b"\x01" * 7)
    g = VoiceFrame.unpack(f.pack())
    assert g == f
    with pytest.raises(ValueError):
        VoiceFrame.unpack(b"XX" + f.pack()[2:])


def test_keepalive_auth():
    raw = make_keepalive(3, 9, "k3y")
    dev, ts, body, mac = parse_keepalive(VoiceFrame.unpack(raw))
    assert dev == 9 and verify_keepalive(body, mac, "k3y", ts)
    assert not verify_keepalive(body, mac, "wrong", ts)
    assert not verify_keepalive(body, mac, "k3y", int(time.time()) - 1000)


def test_ulaw_roundtrip_close():
    pcm = audio.tone(1000, 20, 0.5)
    back = audio.ulaw_to_pcm(audio.pcm_to_ulaw(pcm))
    a, b = audio.pcm_to_samples(pcm), audio.pcm_to_samples(back)
    assert len(a) == len(b) == 160
    assert max(abs(x - y) for x, y in zip(a, b)) < 600  # companding error


def test_mode_codecs_share_ambe2():
    assert codec_for_mode("DMR") == codec_for_mode("NXDN") == codec_for_mode("P25P2") == Codec.AMBE2
    assert codec_for_mode("P25P1") == Codec.IMBE
    assert codec_for_mode("ANALOG") == Codec.ULAW


def test_dvsi_packet_parity():
    pkt = build_packet(PKT_CONTROL, bytes([F_RATET, 34]))
    assert pkt[0] == 0x61
    length = struct.unpack("!H", pkt[1:3])[0]
    assert length == len(pkt) - 4
    p = 0
    for b in pkt[1:-1]:
        p ^= b
    assert pkt[-1] == p


class FakeVocoder(VocoderChannel):
    """Stands in for a DVSI channel: 'encodes' by taking the first N bytes of PCM."""
    def __init__(self, codec, pool):
        self.codec, self.pool, self.name = codec, pool, "dvsi:fake"
        self.n = {Codec.IMBE: 11, Codec.AMBE2: 7}[codec]

    def encode(self, pcm):
        return pcm[: self.n]

    def decode(self, bits):
        return (bits * 64)[:320]

    def release(self):
        self.pool.free += 1


class FakePool(VocoderPool):
    def __init__(self, n):
        super().__init__([])
        self.free = n

    def available(self, codec):
        return codec in (Codec.PCM16, Codec.ULAW) or self.free > 0

    def acquire(self, codec):
        if codec in (Codec.PCM16, Codec.ULAW):
            return super().acquire(codec)
        if self.free <= 0:
            raise VocoderUnavailable("none")
        self.free -= 1
        return FakeVocoder(codec, self)


def test_transcoder_paths():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "central"))
    from app.services.transcoder import TranscoderService

    pool = FakePool(2)
    ts = TranscoderService(pool)
    s = ts.session(1, Codec.IMBE)
    out = s.convert(b"\x05" * 11, {Codec.IMBE, Codec.AMBE2, Codec.ULAW})
    assert out[Codec.IMBE] == b"\x05" * 11          # passthrough
    assert len(out[Codec.AMBE2]) == 7               # IMBE -> PCM -> AMBE+2
    assert len(out[Codec.ULAW]) == 160              # IMBE -> PCM -> G.711
    assert pool.free == 0                           # decoder + encoder leased
    assert ts.close(1) is True and pool.free == 2   # released at call end

    # DMR <-> NXDN <-> P25P2: AMBE+2 passthrough needs no hardware
    s2 = ts.session(2, Codec.AMBE2)
    assert s2.convert(b"\x07" * 7, {Codec.AMBE2}) == {Codec.AMBE2: b"\x07" * 7}
    assert pool.free == 2

    # no hardware -> target dropped, other targets still served
    empty = TranscoderService(FakePool(0))
    s3 = empty.session(3, Codec.ULAW)
    out = s3.convert(audio.pcm_to_ulaw(audio.tone(800, 20)), {Codec.ULAW, Codec.IMBE, Codec.PCM16})
    assert set(out) == {Codec.ULAW, Codec.PCM16}
