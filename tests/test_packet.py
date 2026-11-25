"""Packet-voice codecs: DFSI framing, IPSC framing, P25 Reed-Solomon.

Run:  PYTHONPATH=.:central pytest -q tests
"""
import os
import sys

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..")]

from lmrcommon import dfsi as D  # noqa: E402
from lmrcommon import ipsc as I  # noqa: E402
from lmrcommon import p25rs  # noqa: E402


def test_reed_solomon_lc_and_es():
    lc = D.group_lc(100, 4101, emergency=True)
    coded = p25rs.encode_lc(lc)
    assert len(coded) == 18 and p25rs.syndromes_ok(coded, 12) and p25rs.decode_lc(coded) == lc
    bad = bytearray(coded)
    bad[10] ^= 0x04
    assert not p25rs.syndromes_ok(bytes(bad), 12)
    es = bytes(9) + bytes([0x80, 0, 0])
    assert p25rs.decode_es(p25rs.encode_es(es)) == es


def test_dfsi_stream_round_trip():
    enc, dec = D.VoiceEncoder(100, 4101, emergency=True), D.VoiceDecoder()
    events = dec.feed(enc.start())
    frames = [bytes([i]) * 11 for i in range(18)]
    for f in frames:
        events += dec.feed(enc.voice(f))
    events += dec.feed(enc.end())
    kinds = [e[0] for e in events]
    assert kinds[0] == "start" and ("header", 100) in events and kinds[-1] == "end"
    assert [e[1] for e in events if e[0] == "voice"] == frames
    lc = next(e[1] for e in events if e[0] == "lc")
    assert (lc["tg"], lc["src"], lc["emergency"], lc["private"]) == (100, 4101, True, False)
    assert next(e[1] for e in events if e[0] == "es")["algid"] == D.ALGID_CLEAR
    assert kinds.index("lc") == 2 + 8    # start, header, then 8 voice frames: complete after LDU1 frame 8
    assert D.parse_fsc(D.fsc_connect(15211, 7)) == {"msg": "connect", "version": 1, "vc_port": 15211, "ssrc": 7,
                                                     "fs_hb": 5, "host_hb": 5}
    assert D.parse_fsc(D.fsc_ack(D.FSC_CONNECT))["msg"] == "ack" and D.parse_fsc(D.rtp_packet(1, 2, 3, b"x")) is None


def test_ipsc_auth_voice_and_peer_list():
    key = I.auth_key("1A2B3C")
    pkt = I.sign(I.master_reg_req(311001, I.flags(auth=True)), key)
    assert I.verify(pkt, key) == pkt[:-10] and I.verify(pkt, I.auth_key("FFFF")) is None
    frames = [bytes([0x11 * (i + 1)] * 6 + [0x80]) for i in range(3)]
    v = I.parse_voice(I.voice_packet(9990, 3, 3101, 9, 0xCAFE, 2, I.BURST_TS2, I.burst_body(frames, 1), 10, 480))
    assert (v["src"], v["dst"], v["ts"], v["stream"], v["end"]) == (3101, 9, 2, 0xCAFE, False)
    assert v["frames"] == frames
    t = I.parse_voice(I.voice_packet(9990, 3, 3101, 9, 0xCAFE, 1, I.BURST_TERM,
                                     I.lc_body(I.full_lc(9, 3101), terminator=True), 11, 960, end=True))
    assert t["end"] and t["lc"] == I.full_lc(9, 3101)
    peers = [(311001, "10.1.2.3", 50001, 0x6A), (311002, "10.1.2.4", 50002, 0x6A)]
    assert I.parse_peer_list(I.peer_list_reply(9990, peers)) == peers
