"""P25 DFSI (Digital Fixed Station Interface, TIA-102.BAHA style) framing: control and voice.

KeyUp acts as the host (the "RFSS side") of a DFSI link to a P25 conventional fixed station (a base
station or repeater with an IP DFSI port, or a V.24-to-IP DFSI converter). One UDP socket carries both:

  * Fixed Station Control (FSC) messages -- first byte < 0x80:
        FSC_CONNECT    0x00  version, voice-conveyance base port, SSRC, heartbeat periods
        FSC_HEARTBEAT  0x01  version
        FSC_ACK        0x02  acked message id, version, response code (0 = accepted)
        FSC_DISCONNECT 0x09  version
  * Voice conveyance -- RTP (RFC 3550, version 2, payload type 100) whose payload is a list of
    blocks, each announced by a compact block header (E bit 0x80 = "another header follows",
    7-bit block type), followed by the block bodies in the same order:
        FULL_RATE_VOICE  0   one IMBE voice frame (CAI frame types 0x62..0x73) -- see below
        VOICE_HEADER_1   6   18 bytes: MI(9) MFID(1) ALGID(1) KID(2) TGID(2) reserved(3)
        VOICE_HEADER_2   7   18 bytes (reserved; zero)
        START_OF_STREAM  9   3 bytes: NID(2) error count(1)
        END_OF_STREAM   10   no body

Full-rate voice block: frame type (1) | IMBE voice parameters u0..u7 without FEC (11 bytes = 88 bits,
KeyUp's IMBE network codec) | status (1) | additional frame data:
        0x62 0x63 (LDU1 1-2), 0x6B 0x6C (LDU2 10-11)   none
        0x64..0x69 (LDU1 3-8)                         3 bytes of RS(24,12,13) coded link control
        0x6D..0x72 (LDU2 12-17)                       3 bytes of RS(24,16,9) coded encryption sync
        0x6A, 0x73 (frames 9 and 18)                  4 bytes of low-speed data

The layout follows the TIA-102.BAHA / V.24 frame structure as published descriptions give it. It is
kept in this one module so it can be adjusted to a particular station without touching the rest.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .p25rs import decode_es, decode_lc, encode_es, encode_lc

FSC_CONNECT, FSC_HEARTBEAT, FSC_ACK, FSC_DISCONNECT = 0x00, 0x01, 0x02, 0x09
FSC_VERSION = 1
ACK_OK, ACK_REFUSED = 0, 1

BT_FULL_RATE_VOICE, BT_VOICE_HEADER_1, BT_VOICE_HEADER_2, BT_START_OF_STREAM, BT_END_OF_STREAM = 0, 6, 7, 9, 10
RTP_PT = 100
RTP = struct.Struct("!BBHII")

LDU1_TYPES = list(range(0x62, 0x6B))   # frames 1..9
LDU2_TYPES = list(range(0x6B, 0x74))   # frames 10..18
FRAME_TYPES = LDU1_TYPES + LDU2_TYPES
LCO_GROUP, LCO_PRIVATE = 0x00, 0x03
ALGID_CLEAR = 0x80
NULL_IMBE = bytes.fromhex("040cfd7bfb7df27b3d9e45")   # silence voice parameters


def extra_len(ft: int) -> int:
    if 0x64 <= ft <= 0x69 or 0x6D <= ft <= 0x72:
        return 3
    if ft in (0x6A, 0x73):
        return 4
    return 0


FIXED_LEN = {BT_VOICE_HEADER_1: 18, BT_VOICE_HEADER_2: 18, BT_START_OF_STREAM: 3, BT_END_OF_STREAM: 0}


# ---------------------------------------------------------------- control
def fsc_connect(vc_port: int, ssrc: int, fs_hb: int = 5, host_hb: int = 5) -> bytes:
    return struct.pack("!BBHIBB", FSC_CONNECT, FSC_VERSION, vc_port, ssrc, fs_hb, host_hb)


def fsc_heartbeat() -> bytes:
    return bytes([FSC_HEARTBEAT, FSC_VERSION])


def fsc_ack(msg_id: int, code: int = ACK_OK) -> bytes:
    return bytes([FSC_ACK, msg_id, FSC_VERSION, code])


def fsc_disconnect() -> bytes:
    return bytes([FSC_DISCONNECT, FSC_VERSION])


def parse_fsc(data: bytes) -> dict | None:
    if not data or data[0] >= 0x80:
        return None
    mid = data[0]
    if mid == FSC_CONNECT and len(data) >= 10:
        _, ver, port, ssrc, fs_hb, host_hb = struct.unpack_from("!BBHIBB", data)
        return {"msg": "connect", "version": ver, "vc_port": port, "ssrc": ssrc, "fs_hb": fs_hb, "host_hb": host_hb}
    if mid == FSC_HEARTBEAT:
        return {"msg": "heartbeat"}
    if mid == FSC_ACK and len(data) >= 4:
        return {"msg": "ack", "acked": data[1], "code": data[3]}
    if mid == FSC_DISCONNECT:
        return {"msg": "disconnect"}
    return {"msg": "unknown", "id": mid}


# ---------------------------------------------------------------- RTP + blocks
def rtp_packet(seq: int, ts: int, ssrc: int, payload: bytes, marker: bool = False) -> bytes:
    return RTP.pack(0x80, (0x80 if marker else 0) | RTP_PT, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


def parse_rtp(data: bytes) -> tuple[int, int, bytes] | None:
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    _, pt, seq, ts, ssrc = RTP.unpack_from(data)
    cc = data[0] & 0x0F
    return seq, ssrc, data[12 + 4 * cc:]


def pack_blocks(blocks: list[tuple[int, bytes]]) -> bytes:
    hdrs = bytes((0x80 if i < len(blocks) - 1 else 0) | bt for i, (bt, _) in enumerate(blocks))
    return hdrs + b"".join(body for _, body in blocks)


def unpack_blocks(payload: bytes) -> list[tuple[int, bytes]]:
    types, i = [], 0
    while i < len(payload):
        h = payload[i]
        types.append(h & 0x7F)
        i += 1
        if not h & 0x80:
            break
    out = []
    for bt in types:
        if bt == BT_FULL_RATE_VOICE:
            if i >= len(payload):
                break
            n = 13 + extra_len(payload[i])
        else:
            n = FIXED_LEN.get(bt, 0)
        out.append((bt, payload[i:i + n]))
        i += n
    return out


def group_lc(tg: int, src: int, emergency: bool = False, private: bool = False) -> bytes:
    """Group voice channel user (LCO 0x00) / unit-to-unit (0x03) link control, 9 bytes, MFID 0x00."""
    svc = 0x80 if emergency else 0x00
    if private:
        return bytes([LCO_PRIVATE, 0x00, svc]) + tg.to_bytes(3, "big") + src.to_bytes(3, "big")
    return bytes([LCO_GROUP, 0x00, svc, 0x00]) + tg.to_bytes(2, "big") + src.to_bytes(3, "big")


def parse_lc(lc: bytes) -> dict:
    lco = lc[0] & 0x3F
    if lco == LCO_PRIVATE:
        return {"lco": lco, "private": True, "tg": int.from_bytes(lc[3:6], "big"), "src": int.from_bytes(lc[6:9], "big"),
                "emergency": bool(lc[2] & 0x80)}
    return {"lco": lco, "private": False, "tg": int.from_bytes(lc[4:6], "big"), "src": int.from_bytes(lc[6:9], "big"),
            "emergency": bool(lc[2] & 0x80)}


# ---------------------------------------------------------------- voice stream
@dataclass
class VoiceEncoder:
    """Builds the RTP payloads of one outgoing call (one IMBE frame per packet)."""
    tg: int
    src: int
    emergency: bool = False
    nac: int = 0x293
    n: int = 0
    lc_coded: bytes = b""
    es_coded: bytes = b""

    def __post_init__(self):
        self.lc_coded = encode_lc(group_lc(self.tg, self.src, self.emergency))
        self.es_coded = encode_es(bytes(9) + bytes([ALGID_CLEAR, 0, 0]))

    def start(self) -> bytes:
        vh1 = bytes(9) + bytes([0x00, ALGID_CLEAR, 0, 0]) + self.tg.to_bytes(2, "big") + bytes(3)
        return pack_blocks([(BT_START_OF_STREAM, struct.pack("!HB", self.nac << 4, 0)),
                            (BT_VOICE_HEADER_1, vh1), (BT_VOICE_HEADER_2, bytes(18))])

    def voice(self, imbe: bytes) -> bytes:
        idx = self.n % 18
        ft = FRAME_TYPES[idx]
        extra = b""
        if 0x64 <= ft <= 0x69:
            k = ft - 0x64
            extra = self.lc_coded[3 * k:3 * k + 3]
        elif 0x6D <= ft <= 0x72:
            k = ft - 0x6D
            extra = self.es_coded[3 * k:3 * k + 3]
        elif ft in (0x6A, 0x73):
            extra = bytes(4)
        status = (idx // 9) & 1   # superframe half
        self.n += 1
        return pack_blocks([(BT_FULL_RATE_VOICE, bytes([ft]) + imbe[:11].ljust(11, b"\0") + bytes([status]) + extra)])

    def end(self) -> bytes:
        return pack_blocks([(BT_END_OF_STREAM, b"")])


@dataclass
class VoiceDecoder:
    """Turns received blocks into events: ('start',), ('header', tg), ('voice', imbe), ('lc', dict), ('end',)."""
    lc_parts: dict = field(default_factory=dict)
    es_parts: dict = field(default_factory=dict)
    lc: dict | None = None

    def feed(self, payload: bytes) -> list[tuple]:
        ev = []
        for bt, body in unpack_blocks(payload):
            if bt == BT_START_OF_STREAM:
                self.lc_parts, self.lc = {}, None
                ev.append(("start",))
            elif bt == BT_VOICE_HEADER_1 and len(body) >= 15:
                ev.append(("header", int.from_bytes(body[13:15], "big")))
            elif bt == BT_END_OF_STREAM:
                ev.append(("end",))
            elif bt == BT_FULL_RATE_VOICE and len(body) >= 13:
                ft = body[0]
                ev.append(("voice", body[1:12]))
                if 0x64 <= ft <= 0x69 and len(body) >= 16:
                    self.lc_parts[ft - 0x64] = body[13:16]
                    if len(self.lc_parts) == 6:
                        coded = b"".join(self.lc_parts[k] for k in range(6))
                        self.lc = parse_lc(decode_lc(coded))
                        self.lc_parts = {}
                        ev.append(("lc", self.lc))
                elif 0x6D <= ft <= 0x72 and len(body) >= 16:
                    self.es_parts[ft - 0x6D] = body[13:16]
                    if len(self.es_parts) == 6:
                        es = decode_es(b"".join(self.es_parts[k] for k in range(6)))
                        self.es_parts = {}
                        ev.append(("es", {"algid": es[9], "kid": int.from_bytes(es[10:12], "big")}))
        return ev
