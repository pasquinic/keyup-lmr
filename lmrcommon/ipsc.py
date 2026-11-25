"""MOTOTRBO IP Site Connect (IPSC) framing, as publicly documented by reverse engineering.

IPSC links MOTOTRBO repeaters over UDP. One repeater (or KeyUp) is the master; peers register to it,
fetch the peer list and keep a full mesh alive; voice is sent to every peer. Capacity Plus systems use
the same framing between their repeaters.

Registration / keepalive (opcode, 4-byte peer ID, then):
    0x90 MASTER_REG_REQ     mode(1) flags(4) version(4)
    0x91 MASTER_REG_REPLY   mode(1) flags(4) number of peers(2) version(4)
    0x92 PEER_LIST_REQ      -
    0x93 PEER_LIST_REPLY    length(2) + entries of peer ID(4) IPv4(4) port(2) mode(1)
    0x94 PEER_REG_REQ       version(4)            0x95 PEER_REG_REPLY    version(4)
    0x96 MASTER_ALIVE_REQ   mode flags version    0x97 MASTER_ALIVE_REPLY mode flags version
    0x98 PEER_ALIVE_REQ     mode flags            0x99 PEER_ALIVE_REPLY  mode flags
    0x9A DE_REG_REQ         -                     0x9B DE_REG_REPLY      -
With authentication on, every packet ends with the first 10 bytes of HMAC-SHA1(key, packet), the key
being up to 40 hex digits (left-padded with zeros).

Voice (0x80 group / 0x81 private), byte offsets:
    0      opcode            1-4   originating peer ID      5     IPSC sequence (per call)
    6-8    source radio ID   9-11  destination (TG / radio) 12    call priority / type
    13-16  call stream ID    17    call info: 0x20 = timeslot 2, 0x40 = end of call
    18-29  RTP header (payload type 0x5D voice, 0x5E terminator; marker on the header)
    30     burst type: 0x01 voice LC header, 0x02 voice terminator, 0x0A / 0x8A voice burst TS1 / TS2
    header / terminator: 31-37 info, 38-46 full link control (FLCO, FID, service options, dst, src)
    voice burst:         31 length, 32 burst number, 33-51 three AMBE+2 frames (49 bits each,
                         one spare bit after each: bits 0-48, 50-98, 100-148)

KeyUp's AMBE+2 network codec is exactly one 49-bit frame (7 bytes), so a burst is three network frames.
Like dfsi.py, the layout is kept in this one module so it can be adjusted to particular equipment.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import struct

MASTER_REG_REQ, MASTER_REG_REPLY = 0x90, 0x91
PEER_LIST_REQ, PEER_LIST_REPLY = 0x92, 0x93
PEER_REG_REQ, PEER_REG_REPLY = 0x94, 0x95
MASTER_ALIVE_REQ, MASTER_ALIVE_REPLY = 0x96, 0x97
PEER_ALIVE_REQ, PEER_ALIVE_REPLY = 0x98, 0x99
DE_REG_REQ, DE_REG_REPLY = 0x9A, 0x9B
GROUP_VOICE, PVT_VOICE = 0x80, 0x81
VOICE_OPCODES = (GROUP_VOICE, PVT_VOICE)

VERSION = bytes([0x04, 0x02, 0x04, 0x01])
# mode: operational 0x40 | digital 0x20 | TS1 linked 0x08 | TS2 linked 0x02
MODE = 0x6A
# flags byte 3: 0x04 voice calls, 0x08 data calls, 0x10 authentication; byte 2 bit 0x80 = Capacity Plus peer
FLAG_VOICE, FLAG_DATA, FLAG_AUTH, FLAG_CAPPLUS = 0x04, 0x08, 0x10, 0x80

BURST_HEADER, BURST_TERM, BURST_TS1, BURST_TS2 = 0x01, 0x02, 0x0A, 0x8A
INFO_TS2, INFO_END = 0x20, 0x40
RTP_VOICE, RTP_TERM = 0x5D, 0x5E
FLCO_GROUP, FLCO_PRIVATE = 0x00, 0x03
AMBE_NULL = bytes.fromhex("b9e881526173002a")[:7]   # an AMBE+2 silence frame (49 bits + pad)


def flags(auth: bool = False, capplus: bool = False) -> bytes:
    return bytes([0x00, 0x00, FLAG_CAPPLUS if capplus else 0x00, FLAG_VOICE | FLAG_DATA | (FLAG_AUTH if auth else 0)])


# ---------------------------------------------------------------- authentication
def auth_key(hexkey: str) -> bytes | None:
    hexkey = (hexkey or "").strip()
    return bytes.fromhex(hexkey.rjust(40, "0")) if hexkey else None


def sign(packet: bytes, key: bytes | None) -> bytes:
    if not key:
        return packet
    return packet + hmac.new(key, packet, hashlib.sha1).digest()[:10]


def verify(data: bytes, key: bytes | None) -> bytes | None:
    """The packet without its signature, or None if the signature is wrong."""
    if not key:
        return data
    if len(data) < 11:
        return None
    body, mac = data[:-10], data[-10:]
    return body if hmac.compare_digest(hmac.new(key, body, hashlib.sha1).digest()[:10], mac) else None


# ---------------------------------------------------------------- registration
def _id(peer_id: int) -> bytes:
    return peer_id.to_bytes(4, "big")


def master_reg_req(peer_id: int, fl: bytes, mode: int = MODE) -> bytes:
    return bytes([MASTER_REG_REQ]) + _id(peer_id) + bytes([mode]) + fl + VERSION


def master_reg_reply(master_id: int, fl: bytes, num_peers: int, mode: int = MODE) -> bytes:
    return bytes([MASTER_REG_REPLY]) + _id(master_id) + bytes([mode]) + fl + struct.pack("!H", num_peers) + VERSION


def peer_list_req(peer_id: int) -> bytes:
    return bytes([PEER_LIST_REQ]) + _id(peer_id)


def peer_list_reply(master_id: int, peers: list[tuple[int, str, int, int]]) -> bytes:
    body = b"".join(_id(pid) + ipaddress.IPv4Address(ip).packed + struct.pack("!H", port) + bytes([mode])
                    for pid, ip, port, mode in peers)
    return bytes([PEER_LIST_REPLY]) + _id(master_id) + struct.pack("!H", len(body)) + body


def parse_peer_list(data: bytes) -> list[tuple[int, str, int, int]]:
    n = struct.unpack_from("!H", data, 5)[0]
    out = []
    for i in range(7, 7 + n, 11):
        pid = int.from_bytes(data[i:i + 4], "big")
        ip = str(ipaddress.IPv4Address(data[i + 4:i + 8]))
        port = struct.unpack_from("!H", data, i + 8)[0]
        out.append((pid, ip, port, data[i + 10]))
    return out


def master_alive_req(peer_id: int, fl: bytes, mode: int = MODE) -> bytes:
    return bytes([MASTER_ALIVE_REQ]) + _id(peer_id) + bytes([mode]) + fl + VERSION


def master_alive_reply(master_id: int, fl: bytes, mode: int = MODE) -> bytes:
    return bytes([MASTER_ALIVE_REPLY]) + _id(master_id) + bytes([mode]) + fl + VERSION


def peer_reg_req(peer_id: int) -> bytes:
    return bytes([PEER_REG_REQ]) + _id(peer_id) + VERSION


def peer_reg_reply(peer_id: int) -> bytes:
    return bytes([PEER_REG_REPLY]) + _id(peer_id) + VERSION


def peer_alive_req(peer_id: int, fl: bytes, mode: int = MODE) -> bytes:
    return bytes([PEER_ALIVE_REQ]) + _id(peer_id) + bytes([mode]) + fl


def peer_alive_reply(peer_id: int, fl: bytes, mode: int = MODE) -> bytes:
    return bytes([PEER_ALIVE_REPLY]) + _id(peer_id) + bytes([mode]) + fl


def opcode_peer(data: bytes) -> tuple[int, int]:
    return data[0], int.from_bytes(data[1:5], "big") if len(data) >= 5 else 0


# ---------------------------------------------------------------- AMBE+2 packing
def pack_ambe(frames: list[bytes]) -> bytes:
    """Three 49-bit frames (7 bytes each, MSB first) -> 19 bytes with a spare bit after each frame."""
    v = 0
    for f in (frames + [AMBE_NULL] * 3)[:3]:
        v = (v << 50) | ((int.from_bytes(f[:7], "big") >> 7) << 1)
    return (v << 2).to_bytes(19, "big")   # 150 bits used of 152


def unpack_ambe(data: bytes) -> list[bytes]:
    v = int.from_bytes(data[:19], "big") >> 2
    out = []
    for i in range(3):
        bits49 = (v >> (50 * (2 - i) + 1)) & ((1 << 49) - 1)
        out.append((bits49 << 7).to_bytes(7, "big"))
    return out


# ---------------------------------------------------------------- voice packets
def full_lc(dst: int, src: int, private: bool = False, emergency: bool = False) -> bytes:
    svc = 0x80 if emergency else 0x00
    return bytes([FLCO_PRIVATE if private else FLCO_GROUP, 0x00, svc]) + dst.to_bytes(3, "big") + src.to_bytes(3, "big")


def voice_packet(peer_id: int, ipsc_seq: int, src: int, dst: int, stream: int, ts: int, burst: int, body: bytes,
                 rtp_seq: int, rtp_ts: int, end: bool = False, private: bool = False, priority: int = 0x02) -> bytes:
    info = (INFO_TS2 if ts == 2 else 0) | (INFO_END if end else 0)
    pt = RTP_TERM if burst == BURST_TERM else RTP_VOICE
    rtp = struct.pack("!BBHII", 0x80, pt | (0x80 if burst == BURST_HEADER else 0), rtp_seq & 0xFFFF,
                      rtp_ts & 0xFFFFFFFF, 0)
    hdr = (bytes([PVT_VOICE if private else GROUP_VOICE]) + _id(peer_id) + bytes([ipsc_seq & 0xFF])
           + src.to_bytes(3, "big") + dst.to_bytes(3, "big") + bytes([priority]) + stream.to_bytes(4, "big")
           + bytes([info]))
    return hdr + rtp + bytes([burst]) + body


def lc_body(lc: bytes, terminator: bool = False) -> bytes:
    mask = b"\x99\x99\x99" if terminator else b"\x96\x96\x96"
    return bytes([0x80, 0x0A, 0x80, 0x00, 0x60, 0x00, 0x00]) + lc + mask


def burst_body(frames: list[bytes], burst_no: int) -> bytes:
    return bytes([0x14, 0x40 | (burst_no % 6)]) + pack_ambe(frames)


def parse_voice(data: bytes) -> dict | None:
    if len(data) < 31 or data[0] not in VOICE_OPCODES:
        return None
    info = data[17]
    v = {"private": data[0] == PVT_VOICE, "peer": int.from_bytes(data[1:5], "big"), "seq": data[5],
         "src": int.from_bytes(data[6:9], "big"), "dst": int.from_bytes(data[9:12], "big"),
         "stream": int.from_bytes(data[13:17], "big"), "ts": 2 if info & INFO_TS2 else 1,
         "end": bool(info & INFO_END), "burst": data[30], "frames": [], "lc": None, "emergency": False}
    if v["burst"] in (BURST_HEADER, BURST_TERM) and len(data) >= 47:
        lc = data[38:47]
        v["lc"] = lc
        v["emergency"] = bool(lc[2] & 0x80)
    elif v["burst"] in (BURST_TS1, BURST_TS2) and len(data) >= 52:
        v["frames"] = unpack_ambe(data[33:52])
    return v
